package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/joho/godotenv"
	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	waLog "go.mau.fi/whatsmeow/util/log"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
)

type server struct {
	client    *whatsmeow.Client
	container *sqlstore.Container
	token     string
	phone     string
	pairMu    sync.Mutex
	pairing   bool
}

type apiResponse struct {
	OK     bool   `json:"ok"`
	Error  string `json:"error,omitempty"`
	Status string `json:"status,omitempty"`
	Code   string `json:"code,omitempty"`
	Phone  string `json:"phone,omitempty"`
}

type pairRequest struct {
	Phone string `json:"phone"`
}

type sendRequest struct {
	JID  string `json:"jid"`
	Text string `json:"text"`
}

func main() {
	_ = godotenv.Load(".env", "../.env")
	listenAddr := env("WHATSAPP_LISTEN_ADDR", "127.0.0.1:8787")
	host, _, err := net.SplitHostPort(listenAddr)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		log.Fatal("WHATSAPP_LISTEN_ADDR must bind to an explicit loopback IP")
	}
	token := strings.TrimSpace(os.Getenv("WHATSAPP_SERVICE_TOKEN"))
	if token == "" {
		log.Fatal("WHATSAPP_SERVICE_TOKEN is required")
	}
	phone := strings.TrimSpace(os.Getenv("WHATSAPP_PHONE"))
	dbPath := env("WHATSAPP_SESSION_DB", "whatsmeow.db")
	if err := os.MkdirAll(filepath.Dir(dbPath), 0700); err != nil && filepath.Dir(dbPath) != "." {
		log.Fatalf("create session directory: %v", err)
	}

	container, err := openStore(dbPath)
	if err != nil {
		log.Fatalf("open WhatsApp session store: %v", err)
	}
	defer container.Close()

	device, err := container.GetFirstDevice(context.Background())
	if err != nil {
		log.Fatalf("load WhatsApp device: %v", err)
	}
	client := whatsmeow.NewClient(device, waLog.Noop)
	service := &server{client: client, container: container, token: token, phone: phone}

	if client.Store.ID != nil {
		if err := client.Connect(); err != nil {
			log.Fatalf("connect WhatsApp session: %v", err)
		}
		log.Printf("WhatsApp session connected (%s)", client.Store.ID.String())
	} else {
		log.Println("WhatsApp not paired; use python -m alerts.whatsapp_admin pair")
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", service.health)
	mux.HandleFunc("GET /status", service.auth(service.status))
	mux.HandleFunc("POST /pair", service.auth(service.pair))
	mux.HandleFunc("POST /send", service.auth(service.send))

	listener, err := net.Listen("tcp", listenAddr)
	if err != nil {
		log.Fatalf("listen on %s: %v", listenAddr, err)
	}
	log.Printf("WhatsApp service listening on %s", listenAddr)
	server := &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	log.Fatal(server.Serve(listener))
}

func openStore(path string) (*sqlstore.Container, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	uri := fmt.Sprintf("file:%s?_foreign_keys=on&_busy_timeout=5000", filepath.ToSlash(abs))
	return sqlstore.New(context.Background(), "sqlite3", uri, waLog.Noop)
}

func (s *server) auth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+s.token {
			writeJSON(w, http.StatusUnauthorized, apiResponse{OK: false, Error: "unauthorized"})
			return
		}
		next(w, r)
	}
}

func (s *server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, apiResponse{OK: true, Status: "healthy"})
}

func (s *server) status(w http.ResponseWriter, _ *http.Request) {
	s.pairMu.Lock()
	pairing := s.pairing
	paired := s.client.Store.ID != nil
	s.pairMu.Unlock()
	connected := s.client.IsConnected()

	status := "not_paired"
	if paired && connected {
		status = "connected"
	} else if paired {
		status = "disconnected"
	} else if pairing && connected {
		status = "pairing_pending"
	}
	writeJSON(w, http.StatusOK, apiResponse{OK: true, Status: status})
}

func (s *server) pair(w http.ResponseWriter, r *http.Request) {
	if s.client.Store.ID != nil {
		writeJSON(w, http.StatusConflict, apiResponse{OK: false, Error: "WhatsApp is already paired; remove the linked device before pairing again"})
		return
	}
	var req pairRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, apiResponse{OK: false, Error: "invalid JSON"})
		return
	}
	phone := strings.TrimSpace(req.Phone)
	if phone == "" {
		phone = s.phone
	}
	if phone == "" {
		writeJSON(w, http.StatusBadRequest, apiResponse{OK: false, Error: "phone is required in international format"})
		return
	}

	s.pairMu.Lock()
	if s.pairing || s.client.IsConnected() {
		s.pairMu.Unlock()
		writeJSON(w, http.StatusConflict, apiResponse{OK: false, Error: "pairing already started"})
		return
	}
	s.pairing = true
	s.pairMu.Unlock()

	pairCtx, cancelPair := context.WithTimeout(context.Background(), 160*time.Second)
	qrChan, err := s.client.GetQRChannel(pairCtx)
	if err != nil {
		cancelPair()
		s.resetPairing()
		writeJSON(w, http.StatusInternalServerError, apiResponse{OK: false, Error: "could not initialize WhatsApp pairing"})
		return
	}
	if err := s.client.Connect(); err != nil {
		cancelPair()
		s.resetPairing()
		writeJSON(w, http.StatusBadGateway, apiResponse{OK: false, Error: "could not connect to WhatsApp"})
		return
	}
	select {
	case item, ok := <-qrChan:
		if !ok || item.Event == "error" || item.Event == "timeout" {
			cancelPair()
			s.client.Disconnect()
			s.resetPairing()
			writeJSON(w, http.StatusBadGateway, apiResponse{OK: false, Error: "WhatsApp pairing connection failed"})
			return
		}
	case <-time.After(15 * time.Second):
		cancelPair()
		s.client.Disconnect()
		s.resetPairing()
		writeJSON(w, http.StatusGatewayTimeout, apiResponse{OK: false, Error: "WhatsApp connection did not become ready"})
		return
	case <-r.Context().Done():
		cancelPair()
		s.client.Disconnect()
		s.resetPairing()
		return
	}

	code, err := s.client.PairPhone(
		pairCtx, phone, true, whatsmeow.PairClientChrome, "Chrome (Windows)",
	)
	if err != nil {
		cancelPair()
		s.client.Disconnect()
		s.resetPairing()
		writeJSON(w, http.StatusBadGateway, apiResponse{OK: false, Error: "WhatsApp did not issue a pairing code"})
		return
	}
	go s.monitorPairing(pairCtx, cancelPair, qrChan)
	writeJSON(w, http.StatusOK, apiResponse{
		OK: true, Status: "pairing_pending", Code: code, Phone: phone,
	})
}

func (s *server) monitorPairing(ctx context.Context, cancel context.CancelFunc, qrChan <-chan whatsmeow.QRChannelItem) {
	defer cancel()
	defer s.resetPairing()
	for {
		select {
		case item, ok := <-qrChan:
			if !ok || item.Event == "success" || item.Event == "timeout" || item.Event == "error" || strings.HasPrefix(item.Event, "err-") {
				if ok && item.Event != "success" {
					s.client.Disconnect()
				}
				return
			}
		case <-ctx.Done():
			s.client.Disconnect()
			return
		}
	}
}

func (s *server) resetPairing() {
	s.pairMu.Lock()
	s.pairing = false
	s.pairMu.Unlock()
}

func (s *server) send(w http.ResponseWriter, r *http.Request) {
	if s.client.Store.ID == nil || !s.client.IsConnected() {
		writeJSON(w, http.StatusServiceUnavailable, apiResponse{OK: false, Error: "WhatsApp is not connected"})
		return
	}
	var req sendRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, apiResponse{OK: false, Error: "invalid JSON"})
		return
	}
	jid, err := types.ParseJID(strings.TrimSpace(req.JID))
	if err != nil || jid.IsEmpty() || strings.TrimSpace(req.Text) == "" {
		writeJSON(w, http.StatusBadRequest, apiResponse{OK: false, Error: "valid jid and non-empty text are required"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()
	_, err = s.client.SendMessage(ctx, jid, &waE2E.Message{
		Conversation: &req.Text,
	})
	if err != nil {
		writeJSON(w, http.StatusBadGateway, apiResponse{OK: false, Error: "WhatsApp message send failed"})
		return
	}
	writeJSON(w, http.StatusOK, apiResponse{OK: true, Status: "sent"})
}

func writeJSON(w http.ResponseWriter, status int, value apiResponse) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func env(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

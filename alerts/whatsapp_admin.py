import argparse

from alerts.whatsapp_client import get_status, request_pairing_code


def main():
    parser = argparse.ArgumentParser(prog="python -m alerts.whatsapp_admin")
    parser.add_argument("command", choices=("pair", "status"))
    parser.add_argument("phone", nargs="?", help="phone in international format")
    args = parser.parse_args()

    if args.command == "status":
        payload = get_status()
        print(f"WhatsApp service status: {payload.get('status', 'unknown')}")
        return

    payload = request_pairing_code(args.phone)
    print(f"Linking code: {payload['code']}")
    print(f"On your phone: WhatsApp > Linked devices > Link a device > Link with phone number instead")
    print("Enter the code promptly; WhatsApp pairing codes expire in about 160 seconds.")


if __name__ == "__main__":
    main()

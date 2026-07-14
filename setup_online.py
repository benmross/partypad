"""Authorize this PartyPad desktop with the public service."""

import argparse
import os

from device_auth import authorize_device, default_credential_store


def main(argv=None):
    parser = argparse.ArgumentParser(description="authorize PartyPad online sessions")
    parser.add_argument(
        "--service-url",
        default=os.environ.get("PARTYPAD_SERVICE_URL", "https://partypad.benmross.com"),
        help="PartyPad service URL",
    )
    parser.add_argument("--device-name", help="name shown on the device-management page")
    parser.add_argument("--status", action="store_true", help="show local authorization state")
    parser.add_argument("--forget", action="store_true", help="remove the locally stored credential")
    args = parser.parse_args(argv)
    store = default_credential_store()
    if args.status:
        credential = store.load()
        if credential is None:
            print("This laptop is not authorized.")
        else:
            print(f"Authorized as {credential.device_name}; expires {credential.expires_at}.")
        return
    if args.forget:
        store.delete()
        print("Removed the local PartyPad device credential.")
        return
    credential = authorize_device(
        args.service_url,
        device_name=args.device_name,
        store=store,
    )
    print(f"Authorized {credential.device_name}; PartyPad online mode is ready.")


if __name__ == "__main__":
    main()

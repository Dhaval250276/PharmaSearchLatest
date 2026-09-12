"""Print the environment lines that configure the admin sign-in.

    python tools/make_admin_password.py <username>

The password is read without echoing and never written anywhere; only its
scrypt digest is printed, which is what the server stores.
"""

from __future__ import annotations

import getpass
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.admin_auth import hash_password  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip())
        return 2
    username = sys.argv[1].strip()
    if not username:
        print("A user name is required.")
        return 2

    password = getpass.getpass("Admin password: ")
    if len(password) < 12:
        print("Use at least 12 characters. Nothing was written.")
        return 1
    if password != getpass.getpass("Repeat the password: "):
        print("The two entries did not match. Nothing was written.")
        return 1

    print()
    print("Set these three variables where the server runs, then restart it:")
    print()
    print(f'PHARMASEARCH_ADMIN_USERNAME={username}')
    print(f'PHARMASEARCH_ADMIN_PASSWORD_HASH={hash_password(password)}')
    print(f'PHARMASEARCH_SESSION_SECRET={secrets.token_urlsafe(48)}')
    print()
    print("Keep them out of source control. Changing the session secret signs everyone out.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Desktop device authorization and credential storage for PartyPad."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import secrets
import stat
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from version import PROTOCOL_VERSION, __version__

CLIENT_VERSION = __version__
SERVICE_NAME = "PartyPad"
ACCOUNT_NAME = "desktop-device"


class PollingTooFast(RuntimeError):
    def __init__(self, retry_after: int):
        super().__init__("authorization polling was too fast")
        self.retry_after = retry_after


@dataclass(frozen=True)
class DeviceCredential:
    token: str
    device_id: str
    device_name: str
    expires_at: str


class CredentialStore(Protocol):
    def load(self) -> DeviceCredential | None: ...

    def save(self, credential: DeviceCredential) -> None: ...

    def delete(self) -> None: ...


def config_home(*, platform_name: str | None = None, env: dict[str, str] | None = None) -> Path:
    platform_name = platform_name or os.sys.platform
    env = dict(os.environ if env is None else env)
    home = Path(env.get("HOME", Path.home()))
    if platform_name == "win32":
        return Path(env.get("APPDATA", home / "AppData" / "Roaming")) / "PartyPad"
    if platform_name == "darwin":
        return home / "Library" / "Application Support" / "PartyPad"
    return Path(env.get("XDG_CONFIG_HOME", home / ".config")) / "partypad"


class PrivateFileCredentialStore:
    """Portable fallback used when no supported system credential service is available."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_home() / "device_credential.json"

    def load(self) -> DeviceCredential | None:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"cannot read PartyPad device credential: {exc}") from exc
        if not isinstance(raw, dict) or not all(
            isinstance(raw.get(key), str)
            for key in ("token", "device_id", "device_name", "expires_at")
        ):
            raise RuntimeError(f"invalid PartyPad device credential file: {self.path}")
        return DeviceCredential(**{key: raw[key] for key in DeviceCredential.__dataclass_fields__})

    def save(self, credential: DeviceCredential) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            self.path.parent.chmod(0o700)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}")
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(temporary, flags, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump(asdict(credential), output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                self.path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


class KeyringCredentialStore:
    """Adapter for OS credential services exposed through the optional keyring package."""

    def __init__(self, keyring_module):
        self.keyring = keyring_module

    def load(self) -> DeviceCredential | None:
        value = self.keyring.get_password(SERVICE_NAME, ACCOUNT_NAME)
        if value is None:
            return None
        try:
            raw = json.loads(value)
            return DeviceCredential(**raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("the PartyPad system credential is invalid") from exc

    def save(self, credential: DeviceCredential) -> None:
        self.keyring.set_password(SERVICE_NAME, ACCOUNT_NAME, json.dumps(asdict(credential)))

    def delete(self) -> None:
        try:
            self.keyring.delete_password(SERVICE_NAME, ACCOUNT_NAME)
        except self.keyring.errors.PasswordDeleteError:
            pass


class FallbackCredentialStore:
    """Prefer an OS store while keeping the documented private-file escape hatch."""

    def __init__(self, primary: CredentialStore, fallback: CredentialStore):
        self.primary = primary
        self.fallback = fallback

    def load(self) -> DeviceCredential | None:
        try:
            credential = self.primary.load()
        except Exception:
            credential = None
        return credential or self.fallback.load()

    def save(self, credential: DeviceCredential) -> None:
        try:
            self.primary.save(credential)
        except Exception:
            self.fallback.save(credential)
        else:
            self.fallback.delete()

    def delete(self) -> None:
        try:
            self.primary.delete()
        except Exception:
            pass
        self.fallback.delete()


def default_credential_store() -> CredentialStore:
    fallback = PrivateFileCredentialStore()
    try:
        import keyring

        if keyring.get_keyring().priority > 0:
            return FallbackCredentialStore(KeyringCredentialStore(keyring), fallback)
    except Exception:
        pass
    return fallback


def _request_json(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    authorization: str | None = None,
    timeout=15,
) -> dict:
    headers = {
        "Accept": "application/json",
        "User-Agent": f"PartyPad/{CLIENT_VERSION}",
        "X-PartyPad-Protocol": str(PROTOCOL_VERSION),
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if authorization:
        headers["Authorization"] = authorization
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        error = {}
        try:
            error = json.loads(exc.read().decode(errors="replace"))
            detail = error.get("message") or error.get("error")
        except (ValueError, AttributeError):
            detail = None
        if exc.code == 429 and error.get("error") == "slow_down":
            try:
                retry_after = int(exc.headers.get("Retry-After", "5"))
            except ValueError:
                retry_after = 5
            raise PollingTooFast(max(1, min(retry_after, 30))) from exc
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"authorization service returned HTTP {exc.code}{suffix}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"cannot reach PartyPad authorization service: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("authorization service returned an invalid response")
    return value


def verifier_hash(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def platform_id() -> str:
    return "windows" if os.sys.platform == "win32" else "macos" if os.sys.platform == "darwin" else "linux"


def begin_authorization(service_url: str, device_name: str) -> tuple[dict, str]:
    verifier = secrets.token_urlsafe(32)
    result = _request_json(
        service_url.rstrip("/") + "/api/device/authorizations",
        method="POST",
        body={
            "verifier_hash": verifier_hash(verifier),
            "device_name": device_name,
            "platform": platform_id(),
            "client_version": CLIENT_VERSION,
        },
    )
    required = ("device_code", "user_code", "verification_uri", "expires_in", "interval")
    if any(key not in result for key in required):
        raise RuntimeError("authorization service returned an incomplete device flow")
    return result, verifier


def poll_authorization(service_url: str, device_code: str, verifier: str) -> dict:
    return _request_json(
        service_url.rstrip("/") + f"/api/device/authorizations/{device_code}",
        authorization=f"Verifier {verifier}",
    )


def authorize_device(
    service_url: str,
    *,
    device_name: str | None = None,
    store: CredentialStore | None = None,
    open_browser=webbrowser.open,
    sleep=time.sleep,
) -> DeviceCredential:
    device_name = (device_name or platform.node() or "PartyPad laptop").strip()
    if not device_name or len(device_name) > 80:
        raise ValueError("device name must be 1 to 80 characters")
    flow, verifier = begin_authorization(service_url, device_name)
    activation_url = flow.get("verification_uri_complete") or flow["verification_uri"]
    print(f"Authorize this laptop in your browser using code {flow['user_code']}.")
    open_browser(activation_url)
    deadline = time.monotonic() + min(int(flow["expires_in"]), 900)
    interval = max(1, min(int(flow["interval"]), 30))
    while time.monotonic() < deadline:
        sleep(interval)
        try:
            result = poll_authorization(service_url, flow["device_code"], verifier)
        except PollingTooFast as exc:
            interval = max(interval, exc.retry_after)
            continue
        status_value = result.get("status")
        if status_value == "pending":
            interval = max(interval, min(int(result.get("interval", interval)), 30))
            continue
        if status_value == "denied":
            raise RuntimeError("device authorization was denied")
        if status_value != "authorized":
            raise RuntimeError("authorization service returned an unknown device status")
        device = result.get("device")
        if not isinstance(device, dict) or not all(
            isinstance(result.get("device_token") if key == "device_token" else device.get(key), str)
            for key in ("device_token", "id", "name", "expires_at")
        ):
            raise RuntimeError("authorization service returned an incomplete credential")
        credential = DeviceCredential(
            token=result["device_token"],
            device_id=device["id"],
            device_name=device["name"],
            expires_at=device["expires_at"],
        )
        (store or default_credential_store()).save(credential)
        return credential
    raise RuntimeError("device authorization expired; start again")


def private_file_is_secure(path: Path) -> bool:
    return os.name == "nt" or stat.S_IMODE(path.stat().st_mode) == 0o600

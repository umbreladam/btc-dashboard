"""Request guards and secret storage.

1. Proxy guard. umbrelOS 1.x ran an app_proxy container; umbrelOS 2.x proxies apps from umbreld on the host
   (modules/app-gateway). Neither can inject a custom/secret header into proxied requests (supported
   app_proxy env: APP_HOST, APP_PORT, APP_PROTOCOL, PROXY_AUTH_ADD/WHITELIST/BLACKLIST, PROXY_TRUST_UPSTREAM,
   PROXY_TIMEOUT), and the Umbrel session cookie is stripped before it reaches the app. The strongest
   available check is therefore the TCP peer address: on umbrelOS 2.x the gateway connects from the host side
   of the Docker bridge, i.e. this container's default gateway (10.21.0.1); on 1.x from the app_proxy container.
   Every other container on umbrel_main_network has a different address and is refused.
2. CSRF: unsafe methods need Content-Type: application/json AND the X-Satoshi-Request header (a custom header
   forces a CORS preflight, which this app never answers), and a same-origin Origin header if one is sent.
3. Secrets at rest: Fernet with an app-scoped key file in /data (mode 600).
"""
import ipaddress, os, socket, struct
from cryptography.fernet import Fernet, InvalidToken

CSRF_HEADER = "x-satoshi-request"
CSRF_VALUE = "1"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def default_gateway() -> str | None:
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if p[1] == "00000000" and int(p[3], 16) & 2:
                    return socket.inet_ntoa(struct.pack("<L", int(p[2], 16)))
    except Exception:
        pass
    return None


def trusted_peers() -> set[str]:
    """Peers allowed to talk to the app. TRUSTED_PROXY_IPS (comma list) replaces the auto-detection."""
    env = os.environ.get("TRUSTED_PROXY_IPS", "").strip()
    if env:
        return {str(ipaddress.ip_address(x.strip())) for x in env.split(",") if x.strip()}
    peers = {"127.0.0.1", "::1"}
    gw = default_gateway()
    if gw:
        peers.add(gw)
    for name in filter(None, [os.environ.get("APP_PROXY_HOST")]):  # umbrelOS 1.x app_proxy container
        try:
            peers.update(i[4][0] for i in socket.getaddrinfo(name, None))
        except Exception:
            pass
    return peers


def csrf_ok(method: str, headers, host: str | None) -> bool:
    if method not in UNSAFE:
        return True
    if headers.get(CSRF_HEADER) != CSRF_VALUE:
        return False
    if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return False
    origin = headers.get("origin")
    if origin and origin != "null":
        from urllib.parse import urlsplit
        if urlsplit(origin).netloc.lower() != (headers.get("x-forwarded-host") or host or "").lower():
            return False
    return True


# ---------------- secrets at rest ----------------
def _keyfile(data_dir):
    return os.path.join(data_dir, "app-secret.key")


def fernet(data_dir) -> Fernet:
    path = _keyfile(data_dir)
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    os.chmod(path, 0o600)
    with open(path, "rb") as f:
        return Fernet(f.read().strip())


def encrypt(data_dir, plain: str) -> str:
    return "enc:" + fernet(data_dir).encrypt(plain.encode()).decode() if plain else ""


def decrypt(data_dir, token: str) -> str:
    if not token:
        return ""
    if not token.startswith("enc:"):
        return token  # legacy plaintext (migrated on startup)
    try:
        return fernet(data_dir).decrypt(token[4:].encode()).decode()
    except InvalidToken:
        return ""

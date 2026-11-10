import base64
import getpass
import json
import logging
import os
import re
import shutil
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from importlib import metadata

with warnings.catch_warnings():
    # macOS's bundled Python uses LibreSSL; urllib3 warns about it on import but still works.
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")
    try:
        import requests
        from instagrapi import Client
        from instagrapi.exceptions import (
            BadPassword, ChallengeError, ClientError, ClientNeedsUpgradeError, ClientThrottledError,
            PleaseWaitFewMinutes, ProxyAddressIsBlocked, RateLimitError, TwoFactorRequired,
        )
        # Versions before 3 log in as an Instagram app version Instagram now refuses.
        LOGIN_READY = int(metadata.version("instagrapi").split(".")[0]) >= 3
    except ImportError:
        Client, LOGIN_READY = None, False

for _logger in ("instagrapi", "private_request", "public_request"):
    # Keep instagrapi's log lines out of the terminal; failures are reported in the result box.
    logging.getLogger(_logger).addHandler(logging.NullHandler())

ATTEMPTS = 3  # Tries for a mistyped password or two-factor code.
LOGIN_TIMEOUT = 20  # Seconds to wait for each Instagram response while logging in.
CHECK_FIRST = "Check your account from another account before running this again."
USE_SESSIONID = "If password login keeps failing, run this again and choose session."

RED, BOLD, DIM, ON_RED = "31", "1", "2", "97;41"

NAME = "IG Shadow"
CREDITS = "2OZ"
LOGO = (
    "██╗ ██████╗    ███████╗██╗  ██╗ █████╗ ██████╗  ██████╗ ██╗    ██╗",
    "██║██╔════╝    ██╔════╝██║  ██║██╔══██╗██╔══██╗██╔═══██╗██║    ██║",
    "██║██║  ███╗   ███████╗███████║███████║██║  ██║██║   ██║██║ █╗ ██║",
    "██║██║   ██║   ╚════██║██╔══██║██╔══██║██║  ██║██║   ██║██║███╗██║",
    "██║╚██████╔╝   ███████║██║  ██║██║  ██║██████╔╝╚██████╔╝╚███╔███╔╝",
    "╚═╝ ╚═════╝    ╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝  ╚═════╝  ╚══╝╚══╝ ",
)


# ── Terminal output ─────────────────────────────────────────────────────────────

def style(text: str, *codes: str, stream=None) -> str:
    stream = stream or sys.stdout
    if (not codes or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb"
            or not getattr(stream, "isatty", lambda: False)()):
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def say(mark: str, text: str, *codes: str) -> None:
    print(f"  {style(mark, BOLD, RED)} {style(text, *codes)}")


def warn(text: str) -> None:
    sys.stdout.flush()
    err = sys.stderr
    print(f"  {style('✗', BOLD, RED, stream=err)} {style(text, RED, stream=err)}", file=err)


class Spinner:
    """A turning red spinner, the step Instagram is on and a seconds counter while a request runs.

    The spinner turns at a constant speed only while work is happening; when the work ends the
    line is replaced once by a still "›" line (or removed). Off a terminal it prints the still line.
    """

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    FRAME_SECONDS = 0.08
    # Which login step a request belongs to, by the address it goes to; first match wins.
    STEPS = (
        (("two_step", "two_factor"), "verifying the code"),
        (("challenge",), "confirming it's you"),
        (("send_login_request", "accounts/login", "process_client_data"), "sending your login"),
        (("attestation", "oauth.token", "launcher/sync", "qe/sync"), "securing the connection"),
        (("accounts/logout",), "logging out"),
        (("feed/", "reels_tray", "timeline", "news/", "notifications", "direct_v2"), "loading your account"),
    )
    active = None

    def __init__(self, label: str, keep: bool = True):
        self.label, self.keep, self.step = label, keep, ""
        self.live = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.paused = False

    def __enter__(self) -> "Spinner":
        Spinner.active = self
        self.started = time.monotonic()
        if not self.live:
            if self.keep:
                say("›", self.label)
            return self
        sys.stdout.write("\033[?25l")  # Hide the cursor while the spinner turns.
        self.thread = threading.Thread(target=self._turn, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc) -> bool:
        Spinner.active = None
        if self.live:
            self.done.set()
            self.thread.join()
            sys.stdout.write("\r\033[K\033[?25h")
            sys.stdout.flush()
            if self.keep:
                say("›", self.label)
        return False

    def _turn(self) -> None:
        frame = 0
        while True:
            with self.lock:
                if not self.paused:
                    seconds = int(time.monotonic() - self.started)
                    extra = " · ".join(part for part in (self.step, f"{seconds}s" if seconds >= 2 else "") if part)
                    sys.stdout.write(f"\r\033[K  {style(self.FRAMES[frame % len(self.FRAMES)], BOLD, RED)} "
                                     f"{self.label}" + (f"  {style(extra, DIM)}" if extra else ""))
                    sys.stdout.flush()
            frame += 1
            if self.done.wait(self.FRAME_SECONDS):
                return

    def note(self, url: str) -> None:
        for markers, step in self.STEPS:
            if any(marker in url for marker in markers):
                self.step = step
                return

    def pause(self) -> None:
        with self.lock:
            self.paused = True
            if self.live:
                sys.stdout.write("\r\033[K\033[?25h")
                sys.stdout.flush()

    def resume(self) -> None:
        with self.lock:
            self.paused = False
            if self.live:
                sys.stdout.write("\033[?25l")


def ask(label: str, secret: bool = False) -> str:
    spinner = Spinner.active  # A question in the middle of a request (a security code) pauses it.
    if spinner:
        spinner.pause()
    try:
        prompt = f"  {style('?', BOLD, RED)} {style(label, BOLD)} "
        return (getpass.getpass(prompt) if secret else input(prompt)).strip()
    finally:
        if spinner:
            spinner.resume()


def shade(text: str, stream) -> str:
    """Draw logo blocks in bold red and their shadow strokes in dim red."""
    return "".join(style(run, *((BOLD, RED) if run[0] == "█" else (DIM, RED)), stream=stream)
                   for run in re.findall(r"█+|[^█]+", text))


def credits_line(text: str, stream) -> str:
    body = text.rstrip()
    lead, name = body.rsplit(" ", 1)
    return style(lead, DIM, stream=stream) + " " + style(name, BOLD, RED, stream=stream) + text[len(body):]


def box(title: str, rows: list, stream=None, pad: int = 1, roomy: bool = False) -> None:
    """Print rows inside a red border with a highlighted title.

    Each row is (text, codes). With a tuple of style codes the text wraps to fit the terminal;
    with a painter function (the logo, the credits) the text is drawn exactly as given.
    """
    stream = stream or sys.stdout
    inner = max(24, min(shutil.get_terminal_size((80, 24)).columns - 6 - 2 * pad, 72))
    lines = [("", ())] if roomy else []
    for text, codes in rows:
        if callable(codes):
            lines.append((text, codes))
            continue
        hang = "  " if text[:2] in ("✓ ", "✗ ") else ""
        lines += [(line, codes) for line in textwrap.wrap(text, inner, subsequent_indent=hang) or [""]]
    if roomy:
        lines.append(("", ()))
    width = max([len(line) for line, _ in lines] + [len(title) + 4 - 2 * pad])

    def edge(text: str) -> str:
        return style(text, RED, stream=stream)

    def paint(line: str, codes) -> str:
        line = line.ljust(width)
        return codes(line, stream) if callable(codes) else style(line, *codes, stream=stream)

    side = " " * pad
    out = ["", "  " + edge("╭─") + style(f" {title} ", BOLD, ON_RED, stream=stream)
           + edge("─" * (width + 2 * pad - len(title) - 3) + "╮")]
    out += ["  " + edge("│") + side + paint(line, codes) + side + edge("│") for line, codes in lines]
    out += ["  " + edge("╰" + "─" * (width + 2 * pad) + "╯"), ""]
    print("\n".join(out), file=stream)


def fail(text: str, hint: str = "") -> None:
    sys.stdout.flush()
    rows = [("✗ " + text, (BOLD, RED))]
    if hint:
        rows += [("", ()), (hint, (DIM,))]
    box("FAILED", rows, stream=sys.stderr)


def banner() -> None:
    fits = shutil.get_terminal_size((80, 24)).columns >= len(LOGO[0]) + 10
    logo = [(row, shade) for row in LOGO] if fits else [(NAME.upper(), (BOLD, RED))]
    box(NAME.upper(), logo + [("", ()), ("Hide or show your Instagram followers & following lists", (BOLD,)),
                              ("", ()), (f"-- by {CREDITS}", credits_line)], pad=3, roomy=True)


def choose(question: str, options: list) -> str:
    """Show numbered (name, description) options; return the chosen name."""
    width = max(len(name) for name, _ in options)
    print(f"  {style('?', BOLD, RED)} {style(question, BOLD)}")
    for number, (name, about) in enumerate(options, 1):
        print(f"    {style(str(number), BOLD, RED)}  {style(name.ljust(width), BOLD)}  {style(about, DIM)}")
    while True:
        answer = ask(f"Choose 1-{len(options)}:").lower()
        for number, (name, _) in enumerate(options, 1):
            if answer in (str(number), name, name[0]):
                print()
                return name
        warn(f"Type a number from 1 to {len(options)}.")


# ── Login ───────────────────────────────────────────────────────────────────────

class LoginFailed(Exception):
    def __init__(self, text: str, hint: str = ""):
        super().__init__(text)
        self.text, self.hint = text, hint


def ask_code(label: str) -> str:
    while True:
        code = "".join(ask(label).split())
        if code.isdigit():
            return code
        warn("Enter the digits only.")


def ask_challenge_code(username: str, choice=None) -> str:
    method = {"SMS": "SMS", "EMAIL": "email"}.get(getattr(choice, "name", ""), "SMS or email")
    return ask_code(f"Security code Instagram sent by {method}:")


def refuse_password_change(username: str) -> str:
    raise RuntimeError("Instagram wants a password change. Do it in the Instagram app, then run this again.")


def two_factor_source(info: dict) -> str:
    if info.get("totp_two_factor_on"):
        return "your authenticator app"
    if info.get("whatsapp_two_factor_on"):
        return "WhatsApp"
    phone = info.get("obfuscated_phone_number")
    return f"the SMS sent to the number ending {phone}" if phone else "your authenticator app, SMS or WhatsApp"


def add_timeout(session, seconds: int) -> None:
    send = session.request

    def request(*args, **kwargs):
        kwargs.setdefault("timeout", seconds)
        spinner, url = Spinner.active, args[1] if len(args) > 1 else kwargs.get("url", "")
        if spinner:
            spinner.note(str(url))  # Show which login step this request is.
        return send(*args, **kwargs)

    session.request = request


def keep_app_login_reply(client: "Client") -> None:
    # When the app-style login fails, instagrapi retries with Instagram's older login, which
    # answers "Your version of Instagram is out of date" whatever went wrong. Keep the app-style
    # reply so the real reason ("Can't find account", "Incorrect password") can be shown.
    send = client.bloks_caa_login

    def bloks_caa_login(*args, **kwargs):
        outcome = send(*args, **kwargs)
        client.app_login_reply = client.last_response.text if client.last_response is not None else ""
        return outcome

    client.bloks_caa_login = bloks_caa_login
    client.app_login_reply = ""


def app_login_problem(client: "Client") -> str:
    """Return 'account' or 'password' when the app-style login reply names the problem."""
    reply = getattr(client, "app_login_reply", "")
    if re.search(r"can(?:'|’|\\u2019)?t find (?:your )?account", reply, re.IGNORECASE):
        return "account"
    if re.search(r"incorrect password|password you entered is incorrect", reply, re.IGNORECASE):
        return "password"
    return ""


def new_client() -> "Client":
    client = Client()
    for session in (client.private, client.public):
        session.verify = True  # Always check TLS certificates.
        session.trust_env = False  # Ignore proxy settings from the environment.
        add_timeout(session, LOGIN_TIMEOUT)  # Without a timeout, a stalled request hangs forever.
    client.challenge_code_handler = ask_challenge_code
    client.change_password_handler = refuse_password_change
    keep_app_login_reply(client)
    return client


def two_factor(client: "Client", username: str, password: str) -> None:
    # Instagram's current app login has no separate "submit code" request: the login runs
    # again with the code, so a mistyped code can be re-entered.
    info = client.last_json.get("two_factor_info") if isinstance(client.last_json, dict) else None
    label = f"Two-factor code from {two_factor_source(info or {})}:"
    for attempt in range(1, ATTEMPTS + 1):
        code = ask_code(label)
        try:
            with Spinner("Checking the code…"):
                client.login(username, password, verification_code=code)
            return
        except ClientError as error:
            wrong_code = (isinstance(error, TwoFactorRequired)
                          or "code" in f"{getattr(error, 'error_type', '')} {error}".lower())
            if wrong_code and attempt < ATTEMPTS and not isinstance(error, ChallengeError):
                warn("Instagram rejected that code. Try again.")
                continue
            raise


def login_problem(error: Exception, passwords: list) -> tuple:
    if isinstance(error, ProxyAddressIsBlocked):
        return ("Instagram says that username doesn't exist, or it is blocking logins from this network.",
                "Check the username. " + USE_SESSIONID)
    if isinstance(error, BadPassword):
        return ("Instagram rejected the password.",
                "If the password is right, Instagram may be blocking logins from this network. "
                "Try another network, or run this again and choose session.")
    if isinstance(error, ChallengeError):
        hint = "Open the Instagram app, confirm the login was you, then run this again. "
        return ("Instagram wants to confirm this login.", hint + USE_SESSIONID)
    if isinstance(error, ClientNeedsUpgradeError):
        return ("Instagram didn't accept this login. It replied \"out of date\", which it also says "
                "when it won't log in a script.",
                "Check the login works in the Instagram app. If it does, run this again and choose session.")
    text = " ".join(str(error).split())[:300] or type(error).__name__
    for password in sorted(passwords, key=len, reverse=True):
        text = text.replace(password, "<REDACTED>")
    if isinstance(error, (PleaseWaitFewMinutes, RateLimitError, ClientThrottledError)):
        return (f"Login failed: {text}", "Instagram is limiting requests. Wait a few minutes before trying again.")
    return (f"Login failed: {text}", USE_SESSIONID)


def instagram_login(username: str) -> "Client":
    client = new_client()
    passwords = []
    try:
        for attempt in range(1, ATTEMPTS + 1):
            password = ask("Password:", secret=True)
            if not password:
                warn("Enter your password.")
                continue
            passwords.append(password)
            client.app_login_reply = ""
            try:
                with Spinner(f"Logging in as {style('@' + username, BOLD, RED)}…"):
                    client.login(username, password)
            except TwoFactorRequired:
                two_factor(client, username, password)
            except ClientError as error:
                problem = app_login_problem(client)
                if not isinstance(error, BadPassword) and problem != "password":
                    if problem == "account":
                        raise LoginFailed(f"Instagram can't find an account called @{username}.",
                                          "Check the username and try again.") from None
                    raise
                if attempt == ATTEMPTS:
                    raise LoginFailed(*login_problem(BadPassword(), passwords)) from None
                warn("Instagram rejected that password. Try again.")
                continue
            return client
        raise LoginFailed("No password entered.")
    except ClientError as error:
        raise LoginFailed(*login_problem(error, passwords)) from None
    except requests.Timeout:
        raise LoginFailed("Instagram didn't respond in time.", "Check your connection and try again.") from None
    except requests.RequestException:
        raise LoginFailed("Network/TLS failure during login.", "Check your connection and try again.") from None
    except (LoginFailed, RuntimeError):
        raise
    except Exception as error:  # Anything else instagrapi throws: report it instead of a traceback.
        text = " ".join(f"{type(error).__name__}: {error}".split())[:300]
        for password in sorted(passwords, key=len, reverse=True):
            text = text.replace(password, "<REDACTED>")
        raise LoginFailed(f"Login failed unexpectedly ({text}).", USE_SESSIONID) from None


def logout(client: "Client") -> None:
    try:
        with Spinner("Logging out…", keep=False):
            client.logout()
        say("·", "Logged out of the session this script created.", DIM)
    except Exception:
        say("·", "Couldn't log out; you can end this session in Instagram's Login activity.", DIM)


# ── Settings request ────────────────────────────────────────────────────────────

def check_sessionid(sessionid: str) -> str:
    """Return the account's user ID from a sessionid value, or raise ValueError."""
    if not sessionid or any(ord(c) <= 32 or ord(c) >= 127 or c in ';,"\\' for c in sessionid):
        raise ValueError("Enter only the sessionid cookie value, without 'sessionid='.")
    match = re.fullmatch(r"([1-9][0-9]*)(?::|%3[aA]).+", sessionid)
    if not match:
        raise ValueError("Invalid sessionid format: expected a numeric user ID followed by ':' or '%3A'.")
    return match.group(1)


def hide_lists(sessionid: str, hide: bool = True) -> dict:
    user_id = check_sessionid(sessionid)
    token = base64.b64encode(json.dumps({
        "ds_user_id": user_id,
        "sessionid": sessionid,
        "should_use_header_over_cookies": True,
    }, separators=(",", ":")).encode()).decode("ascii")
    authorization = "Bearer IGT:2:" + token
    name = "IGSettingsBooleanMutation"
    form = {
        "method": "post",
        "format": "json",
        "server_timestamps": "true",
        "locale": "user",
        "fb_api_req_friendly_name": name,
        "client_doc_id": "2669261950526978627082629536",
        "variables": json.dumps({
            "setting_id": "hide_followers_following_list",
            "allow_error_codes": True,
            "callsite": "account_privacy",
            "value": hide,
        }),
    }
    request = urllib.request.Request(
        "https://i.instagram.com/graphql/query",
        data=urllib.parse.urlencode(form).encode(),
        headers={
            "Authorization": authorization,
            "Cookie": f"sessionid={sessionid}; ds_user_id={user_id}",
            "IG-INTENDED-USER-ID": user_id,
            "X-IG-App-ID": "124024574287414",
            "X-Auth-Type": "instagram",
            "User-Agent": "Instagram 453.0.0",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Encoding": "identity",
            "X-GraphQL-Client-Library": "pando",
            "X-FB-Friendly-Name": name,
        },
        method="POST",
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise RuntimeError("Redirect refused; credentials were not forwarded.")

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise RuntimeError("Response too large.")
            return json.loads(raw)
    except urllib.error.HTTPError as error:
        code = error.code
        with error:
            raw = error.read(16385)
        details = {}
        try:
            body = json.loads(raw) if len(raw) <= 16384 else {}
            if isinstance(body, dict):
                for key in ("message", "error_type", "status", "code"):
                    if isinstance(body.get(key), (str, int, bool)):
                        details[key] = body[key]
        except (ValueError, UnicodeError):
            pass
        text = json.dumps(details) if details else "No selected JSON error details."
        secrets = {authorization, token, sessionid, urllib.parse.unquote(sessionid),
                   urllib.parse.quote(sessionid, safe="")}
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                text = text.replace(secret, "<REDACTED>")
        raise RuntimeError(f"HTTP {code}: {text[:1500]}. No retry performed.") from None


def update_setting(sessionid: str, hide: bool) -> tuple:
    """Send the settings request once. Returns (succeeded, message, hint)."""
    action = "enabling" if hide else "disabling"
    try:
        body = hide_lists(sessionid, hide)
        data = body.get("data") if isinstance(body, dict) else None
        root = "xdt_api__v1__settings__set_bool"
        matches = [value for key, value in data.items()
                   if key == root or key.startswith(root + "(")] if isinstance(data, dict) else []
        result = matches[0] if len(matches) == 1 and isinstance(matches[0], dict) else {}
        if (not isinstance(body, dict) or body.get("errors") or body.get("error")
                or body.get("status") == "fail" or result.get("value") is not hide
                or any(result.get(key) for key in (
                    "error_name_value", "errorNameValue", "fallback_message", "fallbackMessage"
                ))):
            raise RuntimeError(f"Instagram did not confirm {action} the setting. No retry performed.")
    except RuntimeError as error:
        return False, str(error), CHECK_FIRST
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, "Network/TLS failure. No retry performed.", CHECK_FIRST
    except json.JSONDecodeError:
        return False, "Invalid JSON response. No retry performed.", CHECK_FIRST
    except ValueError as error:
        return False, str(error), ""
    if hide:
        return True, "Enabled: Instagram returned value=true. Lists are hidden.", ""
    return True, "Disabled: Instagram returned value=false. Lists are visible again.", ""


# ── Main ────────────────────────────────────────────────────────────────────────

def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # Never crash on a terminal that can't show ✓ or ╭.
    args = sys.argv[1:]
    if len(set(args)) != len(args) or not set(args) <= {"--unhide", "--sessionid"}:
        fail("Usage: python3 hide_lists.py [--unhide] [--sessionid]")
        return 2
    banner()

    # The flags are shortcuts: each one skips its menu.
    method = "session" if "--sessionid" in args else choose("How do you want to log in?", [
        ("login", "username & password"),
        ("session", "sessionid cookie from your browser"),
    ])
    cookie_login = method == "session"
    if not cookie_login and (sys.version_info < (3, 11) or not LOGIN_READY):
        running = f"{sys.version_info.major}.{sys.version_info.minor}"
        fail(f"Password login needs Python 3.11 or newer with the requirements installed "
             f"(this is Python {running}).",
             "Set it up as the README shows, or run this again and choose session.")
        return 1

    client = None
    if cookie_login:
        sessionid = ask("Your Instagram sessionid value:", secret=True)
        try:
            check_sessionid(sessionid)
        except ValueError as error:
            fail(str(error))
            return 2
    else:
        username = ask("Instagram username:").lstrip("@")
        if not username:
            fail("Enter your Instagram username.")
            return 2
        try:
            client = instagram_login(username)
        except LoginFailed as error:
            fail(error.text, error.hint)
            return 1
        except RuntimeError as error:
            fail(str(error))
            return 1
        sessionid = client.sessionid or ""
        if not sessionid:
            logout(client)
            fail("Logged in, but Instagram didn't return a session token.", USE_SESSIONID)
            return 1
        say("✓", f"Logged in as {style('@' + client.username, BOLD, RED)}")
    print()

    # The session stays in memory only, so hide and unhide can be repeated without logging in
    # again. It is logged out on exit, Ctrl+C or closed input; nothing is written to a file.
    options = [("hide", "hide your followers & following lists"),
               ("unhide", "show them again"),
               ("exit", "log out and quit" if client else "quit")]
    action = "unhide" if "--unhide" in args else None
    question = "What do you want to do?"
    all_succeeded = True
    try:
        while True:
            action = action or choose(question, options)
            if action == "exit":
                break
            hide = action == "hide"
            with Spinner(("Hiding" if hide else "Showing") + " your follower & following lists…"):
                succeeded, message, hint = update_setting(sessionid, hide)
            if succeeded:
                box("DONE", [("✓ " + message, (BOLD,))])
            else:
                fail(message, hint)
            all_succeeded = all_succeeded and succeeded
            action, question = None, "Anything else?"
    finally:
        if client:
            logout(client)
    return 0 if all_succeeded else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print(file=sys.stderr)
        fail("Interrupted.", "If the update was already sent, it may have been applied. " + CHECK_FIRST)
        raise SystemExit(130)
    except EOFError:
        print(file=sys.stderr)
        fail("No input received.")
        raise SystemExit(1)
    except Exception as error:  # Last resort: a red box instead of a traceback.
        fail(f"Unexpected error ({type(error).__name__}).", CHECK_FIRST)
        raise SystemExit(1)

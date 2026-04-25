import getpass
import os
from pathlib import Path
from typing import Literal, Optional, TypedDict

import keyring
import keyring.errors
import litellm
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
KEYRING_SERVICE = "ai_mcp"

PROVIDERS = ["anthropic", "moonshot", "openai", "deepseek", "gemini", "xai"]
ENV_VAR = {p: f"{p.upper()}_API_KEY" for p in PROVIDERS}

DEFAULT_MODELS = {
    "orchestrator": "anthropic/claude-opus-4-7",
    "spec":         "anthropic/claude-opus-4-7",
    "writer":       "anthropic/claude-sonnet-4-6",
}

_VALIDATE_MODEL = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai":    "gpt-4o-mini",
    "moonshot":  "moonshot-v1-8k",
    "deepseek":  "deepseek-chat",
    "gemini":    "gemini-1.5-flash",
    "xai":       "grok-2-latest",
}


def provider_from_model(model: str) -> str:
    """e.g. 'anthropic/claude-opus-4-7' -> 'anthropic'"""
    return model.split("/")[0]


class StoredKey(TypedDict):
    provider: str
    source: str  # "cli" | "env" | "keyring" | ".env"


def resolve_api_key(provider: str, cli_override: Optional[str] = None) -> Optional[tuple[str, str]]:
    """Returns (key, source) or None. Order: cli_override > env var > keyring > .env file."""
    if cli_override:
        return (cli_override, "cli")
    env_var = ENV_VAR.get(provider)
    if env_var and (val := os.environ.get(env_var)):
        return (val, "env")
    try:
        val = keyring.get_password(KEYRING_SERVICE, provider)
        if val:
            return (val, "keyring")
    except keyring.errors.KeyringError as e:
        raise RuntimeError(f"Keyring unavailable: {e}") from e
    if ENV_PATH.exists() and env_var:
        dotenv_data = dotenv_values(ENV_PATH)
        if dotenv_data.get(env_var):
            return (dotenv_data[env_var], ".env")
    return None


def store_api_key(provider: str, key: str, location: Literal["keychain", "env"]) -> None:
    """Save key to OS keychain or to .env (creates if missing, chmod 600)."""
    if location == "keychain":
        try:
            keyring.set_password(KEYRING_SERVICE, provider, key)
        except keyring.errors.KeyringError as e:
            raise RuntimeError(f"Keyring write failed: {e}") from e
    elif location == "env":
        env_var = ENV_VAR[provider]
        lines: list[str] = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
        new_line = f"{env_var}={key}"
        for i, line in enumerate(lines):
            if line.startswith(f"{env_var}="):
                lines[i] = new_line
                break
        else:
            lines.append(new_line)
        ENV_PATH.write_text("\n".join(lines) + "\n")
        os.chmod(ENV_PATH, 0o600)
    else:
        raise ValueError(f"Unknown storage location: {location}")


def remove_api_key(provider: str) -> list[str]:
    """Remove from keychain AND .env. Return list of locations cleared."""
    cleared: list[str] = []
    try:
        if keyring.get_password(KEYRING_SERVICE, provider):
            keyring.delete_password(KEYRING_SERVICE, provider)
            cleared.append("keychain")
    except keyring.errors.KeyringError:
        pass
    if ENV_PATH.exists() and (env_var := ENV_VAR.get(provider)):
        lines = ENV_PATH.read_text().splitlines()
        new_lines = [l for l in lines if not l.startswith(f"{env_var}=")]
        if len(new_lines) < len(lines):
            ENV_PATH.write_text("\n".join(new_lines) + "\n")
            os.chmod(ENV_PATH, 0o600)
            cleared.append(".env")
    return cleared


def list_stored_providers() -> list[StoredKey]:
    """Walk env vars, keyring, .env — return all providers with a stored key and where it lives."""
    seen: dict[str, str] = {}
    for provider, env_var in ENV_VAR.items():
        if os.environ.get(env_var):
            seen[provider] = "env"
    for provider in PROVIDERS:
        if provider in seen:
            continue
        try:
            if keyring.get_password(KEYRING_SERVICE, provider):
                seen[provider] = "keyring"
        except keyring.errors.KeyringError:
            pass
    if ENV_PATH.exists():
        dotenv_data = dotenv_values(ENV_PATH)
        for provider, env_var in ENV_VAR.items():
            if provider not in seen and dotenv_data.get(env_var):
                seen[provider] = ".env"
    return [StoredKey(provider=p, source=s) for p, s in seen.items()]


def validate_key(provider: str, key: str) -> tuple[bool, str]:
    """Make a 1-token test call via litellm. Returns (ok, message).
    Uses a known small model per provider; catches all litellm exceptions."""
    model_name = _VALIDATE_MODEL.get(provider)
    if not model_name:
        return (False, f"Unknown provider: {provider}")
    try:
        litellm.completion(
            model=f"{provider}/{model_name}",
            messages=[{"role": "user", "content": "Hi"}],
            max_tokens=1,
            api_key=key,
        )
        return (True, "Key is valid")
    except Exception as e:
        return (False, str(e))


def first_run_prompt() -> tuple[str, str]:
    """Interactive: pick provider, prompt for key (hidden), validate, ask storage location."""
    print("No API keys configured. Let's set one up.\n")
    while True:
        print("Pick a provider:")
        for i, p in enumerate(PROVIDERS, 1):
            print(f"  [{i}] {p}")
        raw = input("> ").strip()
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(PROVIDERS):
                provider = PROVIDERS[idx]
                break
        except ValueError:
            pass
        print("Invalid choice, try again.\n")
    while True:
        key = getpass.getpass(f"\nPaste your {provider} API key (input hidden): ")
        print("Validating...", end=" ", flush=True)
        ok, msg = validate_key(provider, key)
        if ok:
            print("✓")
            break
        print(f"✗ ({msg})\nTry again.\n")
    print("\nWhere to store it?")
    print("  [1] OS keychain (recommended)")
    print("  [2] ~/.env (plaintext, repo-root)")
    print("  [3] Don't store — I'll re-paste each session")
    choice = input("> ").strip()
    if choice == "1":
        store_api_key(provider, key, "keychain")
        print("✓ saved to keychain")
    elif choice == "2":
        store_api_key(provider, key, "env")
        print(f"✓ saved to {ENV_PATH}")
    else:
        print("Key not stored.")
    return (provider, key)

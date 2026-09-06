import os
import aiohttp

_session: aiohttp.ClientSession | None = None


def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None:
        _session = aiohttp.ClientSession()
    return _session


async def close_session() -> None:
    global _session
    if _session is not None:
        await _session.close()
        _session = None


async def send_jc_reward(user_id: int, amount: int, reason: str = "vanity_24h") -> bool:
    """POST a reward event to Jarvis's /webhook/vanity endpoint.

    Jarvis validates the shared secret and calls add_credits() directly —
    this is the only communication between the two bots; no shared
    database access is needed. Returns True if Jarvis accepted it.
    """
    url = os.environ.get("JARVIS_WEBHOOK_URL")
    secret = os.environ.get("JARVIS_WEBHOOK_SECRET")
    if not url or not secret:
        print("[webhook] JARVIS_WEBHOOK_URL or JARVIS_WEBHOOK_SECRET not set — skipping reward call.")
        return False

    session = get_session()
    try:
        async with session.post(
            url,
            json={"user_id": user_id, "amount": amount, "reason": reason},
            headers={"X-Vanity-Secret": secret},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                return True
            body = await resp.text()
            print(f"[webhook] Jarvis rejected reward for {user_id}: {resp.status} {body}")
            return False
    except Exception as e:
        print(f"[webhook] Failed to send reward for {user_id}: {e}")
        return False

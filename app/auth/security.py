"""密码采用带随机盐的 scrypt；会话凭证在数据库中仅保存摘要。"""

import hashlib
import secrets


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    derived = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=3, maxmem=64 * 1024 * 1024
    )
    return f"scrypt$32768$8$3${salt}${derived.hex()}"


def password_matches(password: str, stored: str | None) -> bool:
    # 不存在的用户也执行相同计算，避免明显的时序差异。
    salt = stored.split("$")[4] if stored else "0" * 32
    candidate = password_hash(password, salt)
    return secrets.compare_digest(candidate, stored or "0" * len(candidate))

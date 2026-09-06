"""登入行為異常偵測。

與作答行為監控是對稱的：那邊看業務員怎麼填問卷，這邊看帳號怎麼被使用。
兩者都由唯增稽核軌跡推導，使用端無從修改。

訊號刻意只留四個。偵測項目愈多，誤報愈多；誤報一多，洗防人員就不看了，
真正的異常跟著被埋掉——這比沒有這頁更糟。門檻皆為常數，上線後依實際
流量調整，不要為了「看起來偵測得多」而放寬。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..models import AuditEvent, Role, User, as_aware, utcnow

# 台灣不實施日光節約時間，固定時差即為精確值，
# 且不依賴作業系統的時區資料檔（精簡容器映像未必內含）。
TAIPEI = timezone(timedelta(hours=8))

LOOKBACK_DAYS = 30
NIGHT_START_HOUR = 2        # 台北時間
NIGHT_END_HOUR = 6
SHARED_ADDRESS_WINDOW = timedelta(minutes=10)
SHARED_ADDRESS_MIN_ACCOUNTS = 3
PROBE_MIN_ACCOUNTS = 3
PROBE_WINDOW = timedelta(hours=24)
FAILED_BURST = 5            # 與 deps.MAX_FAILED_LOGINS 一致：達此數即遭鎖定

LOGIN_ACTIONS = ("auth.login",)
FAILURE_ACTIONS = ("auth.login_failed", "auth.totp_failed")


@dataclass
class LoginSignal:
    kind: str
    label: str
    detail: str
    at: datetime
    subjects: list[str] = field(default_factory=list)
    severity: int = 1


def _local(moment: datetime) -> datetime:
    return as_aware(moment).astimezone(TAIPEI)


def _events(db: Session, since: datetime) -> list[AuditEvent]:
    return (
        db.query(AuditEvent)
        .filter(AuditEvent.action.in_(LOGIN_ACTIONS + FAILURE_ACTIONS),
                AuditEvent.at >= since)
        .order_by(AuditEvent.at.asc())
        .all()
    )


def _agent_usernames(db: Session) -> set[str]:
    return {u.username for u in db.query(User).filter(User.role == Role.AGENT)}


def _shared_address(logins: list[AuditEvent], agents: set[str]) -> list[LoginSignal]:
    """同一位址在短時間內連續登入多個業務員帳號。

    這正是代填的形狀：主管或助理在同一台裝置上，逐一以業務員帳號登入
    代為填寫。單純同處辦公不會呈現此形狀——同事各自在自己的時間登入，
    不會在十分鐘內連續三個。
    """
    signals = []
    by_ip: dict[str, list[AuditEvent]] = defaultdict(list)
    for event in logins:
        if event.ip and event.entity_id in agents:
            by_ip[event.ip].append(event)

    for ip, events in by_ip.items():
        for index, anchor in enumerate(events):
            window = [e for e in events[index:]
                      if as_aware(e.at) - as_aware(anchor.at) <= SHARED_ADDRESS_WINDOW]
            names = sorted({e.entity_id for e in window if e.entity_id})
            if len(names) >= SHARED_ADDRESS_MIN_ACCOUNTS:
                signals.append(LoginSignal(
                    kind="shared_address",
                    label="同一位址短時間內登入多個業務員帳號",
                    detail=(f"位址 {ip} 於 {SHARED_ADDRESS_WINDOW.seconds // 60} 分鐘內"
                            f"登入 {len(names)} 個業務員帳號。此為代填的典型形狀，"
                            f"請確認是否由本人親自填寫。"),
                    at=as_aware(anchor.at),
                    subjects=names,
                    severity=3,
                ))
                break  # 同一位址只報一次，避免同一件事洗版
    return signals


def _credential_probing(failures: list[AuditEvent]) -> list[LoginSignal]:
    """同一位址對多個不同帳號嘗試失敗：猜密碼或憑證外洩後的試探。"""
    signals = []
    by_ip: dict[str, list[AuditEvent]] = defaultdict(list)
    for event in failures:
        if event.ip:
            by_ip[event.ip].append(event)

    for ip, events in by_ip.items():
        recent = [e for e in events
                  if as_aware(events[-1].at) - as_aware(e.at) <= PROBE_WINDOW]
        names = sorted({e.entity_id for e in recent if e.entity_id})
        if len(names) >= PROBE_MIN_ACCOUNTS:
            signals.append(LoginSignal(
                kind="probing",
                label="同一位址對多個帳號登入失敗",
                detail=f"位址 {ip} 於 24 小時內對 {len(names)} 個不同帳號登入失敗。",
                at=as_aware(recent[-1].at),
                subjects=names,
                severity=3,
            ))
    return signals


def _lockouts(failures: list[AuditEvent]) -> list[LoginSignal]:
    """同一帳號失敗次數達鎖定門檻。"""
    signals = []
    by_user: dict[str, list[AuditEvent]] = defaultdict(list)
    for event in failures:
        if event.entity_id:
            by_user[event.entity_id].append(event)

    for username, events in by_user.items():
        if len(events) >= FAILED_BURST:
            signals.append(LoginSignal(
                kind="lockout",
                label="帳號連續登入失敗",
                detail=(f"{username} 於期間內登入失敗 {len(events)} 次，"
                        f"已達鎖定門檻。可能是忘記密碼，也可能有人在猜。"),
                at=as_aware(events[-1].at),
                subjects=[username],
                severity=2,
            ))
    return signals


def _night_logins(logins: list[AuditEvent]) -> list[LoginSignal]:
    """深夜登入。

    保險招攬在晚間是常態，故不看「下班後」，只看凌晨兩點到六點——
    那個時段的登入不論任何角色都值得問一句。
    """
    signals = []
    for event in logins:
        hour = _local(event.at).hour
        if NIGHT_START_HOUR <= hour < NIGHT_END_HOUR:
            signals.append(LoginSignal(
                kind="night",
                label="深夜登入",
                detail=(f"{event.entity_id} 於台北時間 "
                        f"{_local(event.at).strftime('%Y-%m-%d %H:%M')} 登入。"),
                at=as_aware(event.at),
                subjects=[event.entity_id or ""],
                severity=1,
            ))
    return signals


def scan(db: Session, *, since: datetime | None = None, limit: int = 50) -> list[LoginSignal]:
    """掃描登入行為，回傳最需要注意的訊號。"""
    start = since or utcnow() - timedelta(days=LOOKBACK_DAYS)
    events = _events(db, start)
    logins = [e for e in events if e.action in LOGIN_ACTIONS]
    failures = [e for e in events if e.action in FAILURE_ACTIONS]
    agents = _agent_usernames(db)

    signals = (
        _shared_address(logins, agents)
        + _credential_probing(failures)
        + _lockouts(failures)
        + _night_logins(logins)
    )
    signals.sort(key=lambda s: (-s.severity, -s.at.timestamp()))
    return signals[:limit]

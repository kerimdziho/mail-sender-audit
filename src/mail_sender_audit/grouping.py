"""Group scanned messages by sender address or domain (pure, no I/O)."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable

from mail_sender_audit.models import GroupBy, ScannedMessage, SenderGroup, UnsubscribeTarget


def group_messages(
    messages: Iterable[ScannedMessage], by: GroupBy = GroupBy.ADDRESS
) -> tuple[SenderGroup, ...]:
    """Bucket messages and sort groups by size (largest first), then by key."""
    buckets: defaultdict[str, list[ScannedMessage]] = defaultdict(list)
    for message in messages:
        key = message.sender.address if by is GroupBy.ADDRESS else message.sender.domain
        buckets[key].append(message)

    groups = [_build_group(key, bucket, by) for key, bucket in buckets.items()]
    groups.sort(key=lambda group: (-group.count, group.key))
    return tuple(groups)


def _build_group(key: str, bucket: list[ScannedMessage], by: GroupBy) -> SenderGroup:
    ordered = tuple(sorted(bucket, key=lambda m: m.received, reverse=True))

    if by is GroupBy.DOMAIN:
        label = key
    else:
        names = Counter(m.sender.name for m in ordered if m.sender.name)
        # Ties resolve to the first-inserted name, i.e. the one on the newest message.
        label = names.most_common(1)[0][0] if names else key

    targets: list[UnsubscribeTarget] = []
    seen_addresses: set[str] = set()
    for message in ordered:  # newest first: its URLs carry the freshest tokens
        if message.unsubscribe is None or message.sender.address in seen_addresses:
            continue
        seen_addresses.add(message.sender.address)
        targets.append(UnsubscribeTarget(address=message.sender.address, info=message.unsubscribe))

    return SenderGroup(key=key, label=label, messages=ordered, targets=tuple(targets))

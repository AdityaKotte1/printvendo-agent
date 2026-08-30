"""Which physical printer a job goes to, when a shop has several.

A SaaS xerox shop runs two mono machines and a colour one off a single agent.
The kiosk is still one kiosk and the queue is still one queue -- the server has
no business knowing how many machines are behind a counter -- so the choice is
made here, per job, from the options the student paid for.

**Colour never falls back to mono.** A colour job on a mono machine comes out
grey, and the student paid for colour: that is a refund and a complaint, where
waiting is merely a wait. Mono deliberately does not fall back to colour either,
for the mirror reason -- it would print correctly and quietly spend the owner's
colour toner at the mono price. If the right pool is empty the job waits for the
machine it was sold on.

**Busy means work in front of it, not broken.** The pool is ordered by how much
each machine already has queued, so two mono printers share a rush and the
second one takes the job while the first is mid-ream. Ties go to the order the
owner listed them, which makes a single-printer pool deterministic and lets an
owner say which machine they would rather wear out first.
"""

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Pools:
    """The machines at this shop, by what they can print."""

    bw: list[str] = field(default_factory=list)
    colour: list[str] = field(default_factory=list)
    # What a shop with one machine has always had. Every kiosk in the field is
    # configured this way, so it stays the answer when no pool is set rather
    # than being migrated to a one-item list nobody asked for.
    fallback: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.bw or self.colour)


def pool_for(pools: Pools, *, colour: bool) -> list[str]:
    """The machines that may take this job, in the owner's preferred order."""
    if not pools.configured:
        return [pools.fallback] if pools.fallback else []

    chosen = pools.colour if colour else pools.bw
    named = [name for name in chosen if name]
    if named:
        return named

    # One side configured and not the other: a shop that listed colour machines
    # and no mono ones prints everything on the colour machines, because that is
    # plainly what it has. The reverse -- colour work with only mono machines --
    # returns nothing, and the caller refuses the job rather than printing it
    # in grey.
    if colour:
        return []
    return [name for name in pools.colour if name]


def pick(pool: list[str], depth_of: Callable[[str], int]) -> str | None:
    """The machine with least in front of it, ties going to the listed order.

    `depth_of` is passed in rather than read here so the rule can be tested
    without a printer, and so a queue that cannot be read costs one job rather
    than the whole pass -- an unreadable queue is reported as deep, which moves
    work to a machine that can be asked.
    """
    if not pool:
        return None

    def weight(name: str) -> tuple[int, int]:
        try:
            depth = depth_of(name)
        except Exception:  # noqa: BLE001 - an unaskable printer is not a chosen one
            depth = 10**6
        return (depth, pool.index(name))

    return min(pool, key=weight)

"""Sliding window over a 32-bit frame counter using 64-bit extended sequence numbers.

The wire counter is 32 bits wide and wraps naturally.  On receipt we do not know
which *epoch* (how many times the counter has wrapped) a frame belongs to, so we
disambiguate by picking, among the epoch-adjacent candidates, the extended
sequence number closest to the highest one accepted so far:

    candidate(k) = counter + k * 2**32      for k in (epoch-1, epoch, epoch+1)

Rules enforced here (pure, side-effect free so they are trivially testable):

* the very first frame of a link initialises the window at its counter;
* two candidates equidistant from the highest (exactly 2**31 apart) -> reject;
* frames older than ``highest - (WINDOW_SIZE - 1)`` have fallen out of the
  window -> reject (expired);
* a frame whose bit is already set -> reject (duplicate);
* a new, strictly higher sequence number slides the bitmap window forward;
  a frame inside the window but below the highest simply sets its bit.
"""

COUNTER_MOD = 1 << 32          # 32-bit unsigned counter
COUNTER_MAX = COUNTER_MOD - 1
HALF_SPAN = 1 << 31           # equidistant / ambiguity threshold
WINDOW_SIZE = 64              # bitmap covers highest-63 .. highest


def _candidate_epochs(epoch_hint):
    """Epoch-adjacent candidates around the epoch of the current highest."""
    return (epoch_hint - 1, epoch_hint, epoch_hint + 1)


def extend_counter(counter, highest):
    """Expand a 32-bit ``counter`` to the 64-bit seq nearest ``highest``.

    ``highest`` is ``None`` for the first frame: the counter is then taken at
    face value (epoch 0).  Returns ``(extended, ambiguous)``; ``ambiguous`` is
    True when the two closest candidates are exactly 2**31 away.
    """
    if highest is None:
        return counter, False

    epoch_hint = highest // COUNTER_MOD
    best = None
    best_dist = None
    ambiguous = False
    for epoch in _candidate_epochs(epoch_hint):
        candidate = counter + epoch * COUNTER_MOD
        dist = abs(candidate - highest)
        if best_dist is None or dist < best_dist:
            best = candidate
            best_dist = dist
            ambiguous = False
        elif dist == best_dist:
            ambiguous = True
    return best, ambiguous


def _bit_offset(extended, highest):
    """Bitmap offset of ``extended`` relative to ``highest`` (0 = highest)."""
    return highest - extended


def decide(counter, highest, bitmap):
    """Classify a frame against the current window state.

    ``highest`` / ``bitmap`` are ``None`` / ``0`` before the first frame.
    Returns ``(status, new_highest, new_bitmap)`` where status is one of
    ``accepted``, ``duplicate``, ``expired``, ``rejected``.  A ``rejected``
    status means the two epoch-adjacent candidates were exactly 2**31 away
    (distance tie): the frame cannot be placed unambiguously.
    """
    counter &= COUNTER_MAX

    if highest is None:
        # First frame initialises the window.
        return "accepted", counter, 1 << 0

    extended, ambiguous = extend_counter(counter, highest)
    if ambiguous:
        return "rejected", highest, bitmap

    if extended > highest:
        shift = extended - highest
        if shift >= WINDOW_SIZE:
            # Higher than the window but the gap discards the old bitmap;
            # the frame itself is still accepted (a forward jump slides it
            # fully out).  Such a gap is normal after long silence.
            new_bitmap = 1 << 0
        else:
            new_bitmap = ((bitmap << shift) | 1) & ((1 << WINDOW_SIZE) - 1)
        return "accepted", extended, new_bitmap

    offset = _bit_offset(extended, highest)
    if offset >= WINDOW_SIZE:
        return "expired", highest, bitmap

    bit = 1 << offset
    if bitmap & bit:
        return "duplicate", highest, bitmap

    return "accepted", highest, bitmap | bit


def recent_positions(highest, bitmap, limit=WINDOW_SIZE):
    """Return the up-to-``limit`` accepted extended seq numbers, newest first.

    Bit 0 is ``highest``; only set bits are emitted.
    """
    if highest is None:
        return []
    positions = []
    for offset in range(min(WINDOW_SIZE, limit)):
        if bitmap & (1 << offset):
            positions.append(highest - offset)
    return positions


def _project(highest, bitmap, target_highest):
    """Project a bitmap anchored at ``highest`` onto ``target_highest``.

    ``target_highest`` must be >= ``highest`` (callers project onto the
    higher of the two anchors).  Returns a bitmap relative to
    ``target_highest`` (bit 0 = target).  Bits that land at offset
    >= WINDOW_SIZE have fallen outside the target window and are dropped
    here, rather than being carried back into the acceptable range.
    """
    delta = target_highest - highest
    if delta >= WINDOW_SIZE:
        return 0
    return (bitmap << delta) & ((1 << WINDOW_SIZE) - 1)


def merge_windows(h_a, bitmap_a, h_b, bitmap_b):
    """Merge two same-origin station windows after a reconnect.

    A secondary station is forked from an explicit snapshot of the primary,
    so both bitmaps describe extended sequence numbers in the *same* epoch
    coordinate system; the snapshot's base is the common reference.  Each
    bitmap is projected onto the higher of the two highest sequence numbers
    and the projections are unioned.  Positions that project outside the
    resulting ``highest-63 .. highest`` window are discarded.

    Returns ``(merged_highest, merged_bitmap, added_a, added_b)`` where
    ``added_a`` lists positions B had and A was missing, and ``added_b``
    lists positions A had and B was missing; both are restricted to the
    merged window and ordered newest first.  When one side has never seen a
    frame, the other side is returned verbatim with no additions.
    """
    mask = (1 << WINDOW_SIZE) - 1
    if h_a is None:
        return h_b, bitmap_b & mask, [], []
    if h_b is None:
        return h_a, bitmap_a & mask, [], []

    target = max(h_a, h_b)
    proj_a = _project(h_a, bitmap_a, target)
    proj_b = _project(h_b, bitmap_b, target)
    merged = (proj_a | proj_b) & mask

    def _positions(extra_bits):
        return [target - offset for offset in range(WINDOW_SIZE)
                if extra_bits & (1 << offset)]

    added_a = _positions(proj_b & ~proj_a & mask)   # B fills gaps in A
    added_b = _positions(proj_a & ~proj_b & mask)   # A fills gaps in B
    return target, merged, added_a, added_b

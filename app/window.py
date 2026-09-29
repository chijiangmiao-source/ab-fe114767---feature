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
WINDOW_MASK = (1 << WINDOW_SIZE) - 1


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


def project_bitmap(bitmap, old_highest, new_highest):
    """Project a bitmap anchored at ``old_highest`` onto ``new_highest``.

    Both extended sequence numbers live in the same epoch frame of reference
    (calibrated against the common snapshot when a secondary station is
    created), so this is pure integer alignment, never nearest-epoch guessing.
    Positions more than ``WINDOW_SIZE - 1`` behind ``new_highest`` fall out of
    the window and are masked off rather than carried back into range.
    """
    delta = new_highest - old_highest
    if delta >= 0:
        return (bitmap << delta) & WINDOW_MASK
    return bitmap >> (-delta)


def merge_windows(highest_a, bitmap_a, highest_b, bitmap_b):
    """Converge two stations derived from the same snapshot into one window.

    Each bitmap is projected onto the higher of the two highest extended
    sequence numbers and unioned; bit 0 (that highest) is always set.
    Positions that have slid out of the 64-wide window are dropped rather than
    carried back into the acceptable range.  An empty station (no frames since
    the snapshot) contributes nothing.

    Returns ``(merged_highest, merged_bitmap, added)`` where ``added`` is the
    sorted list (newest first) of accepted extended positions contributed by
    the *other* station that were missing from the station at ``highest_a``.
    """
    empty_a = highest_a is None
    empty_b = highest_b is None
    if empty_a and empty_b:
        return None, 0, []
    if empty_a:
        return highest_b, bitmap_b & WINDOW_MASK, recent_positions(highest_b, bitmap_b)
    if empty_b:
        return highest_a, bitmap_a & WINDOW_MASK, []

    if highest_a >= highest_b:
        top, base_h, base_bm, other_h, other_bm = (
            highest_a, highest_a, bitmap_a, highest_b, bitmap_b)
    else:
        top, base_h, base_bm, other_h, other_bm = (
            highest_b, highest_a, bitmap_a, highest_b, bitmap_b)

    projected_other = project_bitmap(other_bm, other_h, top)
    projected_base = project_bitmap(base_bm, base_h, top)
    merged = (projected_base | projected_other | 1) & WINDOW_MASK

    # Positions the other station fills in beyond what station A already had;
    # reported newest first, window positions only.
    added_bm = projected_other & ~projected_base & WINDOW_MASK
    added = [top - off for off in range(WINDOW_SIZE) if added_bm & (1 << off)]
    return top, merged, added

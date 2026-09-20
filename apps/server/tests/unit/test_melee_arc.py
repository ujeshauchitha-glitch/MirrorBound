"""A melee swing hits bodies, not points.

`Hitbox.overlaps_circle` accounted for the target's radius in its distance
test and then ignored it in its angle test, comparing only the direction of
the target's *centre* against the arc. Every enemy was therefore a point as
far as the angle was concerned.

Measured with bare hands (a 97 degree arc, so 49 degrees either side): every
archetype cut off at exactly 48 degrees whatever its size -- the tell, since a
Husk Scarab is drawn 96 units across and a Hollow Archer 28. The scarab, whose
body spans 53 degrees at melee range, lost 62 degrees of swing: you stood
beside it, swung through its middle, and missed.
"""

import math

import pytest

from mirrorbound.game.combat.hitbox import Hitbox, HitboxShape
from mirrorbound.game.entities.entity import Vec2

ARC = 1.7          # bare hands, radians
REACH = 48.0


def _arc(direction: float = 0.0) -> Hitbox:
    return Hitbox(shape=HitboxShape.ARC, position=Vec2(0, 0), size=REACH,
                  direction=direction, arc_angle=ARC)


def _at(angle_deg: float, dist: float) -> Vec2:
    a = math.radians(angle_deg)
    return Vec2(math.cos(a) * dist, math.sin(a) * dist)


def test_a_wide_body_is_hit_beyond_the_arcs_own_half_angle():
    """Its centre is outside the arc; most of it is not."""
    hb = _arc()
    half = math.degrees(ARC) / 2                   # 48.7
    assert hb.overlaps_circle(_at(80, 60), 48), "a scarab-sized body at 80 deg"
    assert not hb.overlaps_circle(_at(80, 60), 4), "a point at 80 deg must still miss"
    assert 80 > half, "the test is only meaningful outside the bare arc"


def test_the_reach_scales_with_the_target_and_is_not_one_constant():
    """The old bug's signature was every enemy cutting off at the same angle."""
    hb = _arc()
    def widest(radius: float) -> int:
        return max(a for a in range(0, 181) if hb.overlaps_circle(_at(a, 60), radius))
    wide, narrow = widest(48), widest(14)
    assert wide > narrow + 25, (wide, narrow)


def test_nothing_behind_you_is_ever_hit():
    hb = _arc()
    for radius in (4, 14, 48):
        assert not hb.overlaps_circle(_at(180, 60), radius)
        assert not hb.overlaps_circle(_at(140, 60), radius)


def test_a_target_out_of_reach_misses_however_wide_it_is():
    hb = _arc()
    assert not hb.overlaps_circle(_at(0, REACH + 60), 48)
    assert hb.overlaps_circle(_at(0, REACH + 40), 48), "just inside reach + radius"


def test_a_body_the_swing_starts_inside_always_connects():
    """Overlapping the attacker: there is no meaningful direction to it."""
    hb = _arc()
    for angle in (0, 90, 179):
        assert hb.overlaps_circle(_at(angle, 10), 40)


@pytest.mark.parametrize("facing_deg", [0, 45, 90, 180, 270, 315])
def test_the_arc_points_where_the_attacker_faces(facing_deg):
    hb = _arc(math.radians(facing_deg))
    assert hb.overlaps_circle(_at(facing_deg, 40), 10), "straight ahead"
    behind = (facing_deg + 180) % 360
    assert not hb.overlaps_circle(_at(behind, 40), 10), "straight behind"

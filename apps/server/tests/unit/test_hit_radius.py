"""Damage radius and physics radius are two different things.

`EnemyDef.size` was doing both jobs. That worked while enemies were round
procedural blobs; Logesh's sheets are not round -- the Husk Scarab is drawn
96x35, the Gloom Hound 91x44 -- and the sizes were never re-tuned when the art
replaced the placeholders. Measured on main before the split, a straight arrow
registered on only 27% of a scarab's visible width, 35% of a hound's and 48%
of the Mirror's: shots went through the drawn body and missed.

Widening `size` alone would have fixed the aiming and broken the swarm, since
the same number spaces crowding enemies apart.
"""

from mirrorbound.api.session import GameSession
from mirrorbound.game.entities.enemy import ARCHETYPES, get_archetype
from mirrorbound.game.entities.entity import Vec2
from tests.conftest import DT


def test_no_enemy_is_harder_to_hit_than_its_physics_body():
    for name, d in ARCHETYPES.items():
        assert d.hit_radius is not None, f"{name} has no hit_radius"
        assert d.hit_radius >= d.size, (name, d.hit_radius, d.size)


def test_hit_radius_defaults_to_size_when_unset():
    """Anything added later without one keeps the old single-number behaviour."""
    from dataclasses import replace
    from mirrorbound.game.entities.enemy import SKELETON, Enemy

    plain = replace(SKELETON, hit_radius=None)
    e = Enemy(id="e1", position=Vec2(), enemy_def=plain)
    assert e.hit_radius == e.radius == plain.size


def test_elite_scales_both_radii_together():
    base, elite = get_archetype("skeleton"), get_archetype("elite_skeleton")
    assert elite.size == base.size * 1.25
    assert elite.hit_radius == base.hit_radius * 1.25


def _shot_lands(kind: str, lateral: float) -> bool:
    """Fire one arrow straight past a pinned enemy `lateral` units off-centre."""
    s = GameSession(f"hit-{kind}-{lateral}", seed=3, record=False)
    st = s.state
    st.enemies = []
    at = st.player.position + Vec2(300, lateral)
    e = st.spawn_enemy(kind, at)
    e.max_health = e.health = 10**9
    st.player.inventory.add_weapon("hunter_bow")
    st.player.inventory.equip("hunter_bow")
    st.player.face(Vec2(1, 0))
    st.player.attack_cooldown = 0
    hits: list[int] = []
    st.bus.subscribe("DAMAGE_DEALT", lambda ev: hits.append(1))
    s.combat.process_player_attack(st)
    for _ in range(100):
        s.step(DT)
        # Pinned: the fast ones otherwise run into the arrow and fake a hit.
        e.position = at.copy()
        e.velocity = Vec2()
        e.health = 10**9
        if hits:
            return True
    return False


def test_a_shot_at_a_wide_enemys_flank_connects():
    """30 units off a scarab's centre is well inside a body drawn 96 across.
    It missed before the split."""
    assert _shot_lands("scarab", 30)
    assert _shot_lands("hound", 30)
    assert _shot_lands("slime", 30)
    assert _shot_lands("brute", 35)
    assert _shot_lands("mirror", 25)


def test_a_shot_well_clear_of_the_body_still_misses():
    """The circle grew to match the art, not to become a homing field."""
    assert not _shot_lands("scarab", 70)
    assert not _shot_lands("archer", 40)
    assert not _shot_lands("skeleton", 45)


def test_swarming_still_packs_tightly():
    """Separation reads `size`, so a swarm is unchanged by the bigger damage
    circle. If it read hit_radius, scarabs would shove each other apart from
    more than five times the distance and stop being a swarm at all."""
    s = GameSession("swarm", seed=3, record=False)
    st = s.state
    st.enemies = []
    for i in range(6):
        st.spawn_enemy("scarab", st.player.position + Vec2(200 + i * 4, i * 4))
    for _ in range(180):
        s.step(DT)

    alive = st.get_active_enemies()
    assert len(alive) >= 2
    gaps = [
        (a.position - b.position).length()
        for i, a in enumerate(alive) for b in alive[i + 1:]
    ]
    closest = min(gaps)
    scarab = get_archetype("scarab")
    # Packed to roughly their physical size, nowhere near their damage size.
    assert closest < scarab.hit_radius, (closest, scarab.hit_radius)
    assert closest <= scarab.size * 2 + 6, (closest, scarab.size)

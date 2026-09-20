"""Collision system: projectile-vs-entity overlap tests.

Melee arcs are resolved by the combat system at swing time; this only
handles things that travel.
"""

from __future__ import annotations

from mirrorbound.game.combat.combat import CombatSystem
from mirrorbound.game.core.events import EventBus
from mirrorbound.game.entities.entity import Entity
from mirrorbound.game.entities.projectile import Projectile
from mirrorbound.game.state import GameState


class CollisionSystem:
    def __init__(self, bus: EventBus, combat: CombatSystem):
        self.bus = bus
        self.combat = combat

    def update(self, dt: float, state: GameState) -> None:
        for projectile in state.projectiles:
            if not projectile.active:
                continue
            if projectile.faction == "ally":
                for enemy in state.get_active_enemies():
                    if enemy.id in projectile.hit_ids:
                        continue
                    if self._overlap(projectile, enemy):
                        self.combat.projectile_hit_enemy(state, projectile, enemy)
                        if not projectile.active:
                            break
            else:
                for target in (state.player, state.twin):
                    if target.id in projectile.hit_ids:
                        continue
                    if target.id == state.twin.id and state.twin.downed:
                        continue
                    if target.id == state.player.id and state.player.state == "dead":
                        continue
                    if self._overlap(projectile, target):
                        self.combat.projectile_hit_ally(state, projectile, target)
                        if not projectile.active:
                            break
        state.projectiles = [p for p in state.projectiles if p.active]

    @staticmethod
    def _overlap(a: Projectile, b: Entity) -> bool:
        # An enemy is hit against its damage radius; the player and the twin
        # have only the one radius, so `getattr` covers both without the
        # collision system having to know which is which.
        target = getattr(b, "hit_radius", b.radius)
        return (a.position - b.position).length() < a.radius + target

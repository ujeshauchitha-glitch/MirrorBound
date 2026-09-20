"""Executes validated `TwinIntent`s and reports their outcomes.

The controller says *what* it wants (INTERCEPT enemy_12 at (x, y)); this turns
that into velocity and attack calls, checks that the target still exists, and
publishes TWIN_ACTION when the intent changes and TWIN_OUTCOME when it ends,
so the twin's style model can learn from what actually happened.
"""

from __future__ import annotations

from dataclasses import dataclass

from mirrorbound.game.combat.combat import CombatSystem
from mirrorbound.game.entities.enemy import Enemy
from mirrorbound.game.entities.entity import Vec2
from mirrorbound.game.entities.twin import TwinIntent
from mirrorbound.game.state import GameState

OFFENSIVE = {"ATTACK", "ASSIST", "INTERCEPT", "PROTECT", "DISTRACT", "FLANK", "COMBO"}
MAX_INTENT_SECONDS = 4.0
MIN_OUTCOME_SECONDS = 0.4


@dataclass
class _Open:
    intent: TwinIntent
    started_tick: int
    dealt_at_start: float
    taken_at_start: float
    kills_at_start: int


class TwinExecutor:
    def __init__(self) -> None:
        self._open: _Open | None = None

    # --- intent lifecycle -------------------------------------------------------

    def on_intent(self, state: GameState, intent: TwinIntent) -> None:
        current = self._open
        changed = (
            current is None
            or current.intent.intent_type != intent.intent_type
            or current.intent.target_id != intent.target_id
        )
        expired = current is not None and (state.tick - current.started_tick) / 60.0 > MAX_INTENT_SECONDS
        if changed or expired:
            if current is not None:
                self._close(state, current, reason="changed" if changed else "expired")
            self._open = _Open(intent, state.tick, state.twin.damage_dealt, state.twin.damage_taken, state.twin.kills)
            state.emit(
                "TWIN_ACTION",
                intent=intent.intent_type,
                target=intent.target_id,
                position=intent.position.to_dict() if intent.position else None,
                confidence=round(intent.confidence, 3),
                utilities=dict(intent.utilities),
                reason=intent.reason,
                twin_position=state.twin.position.to_dict(),
            )
        state.twin.remember(intent)
        self._apply_weapon_switch(state, intent)

    def _apply_weapon_switch(self, state: GameState, intent: TwinIntent) -> None:
        """The controller only ever suggests a weapon via `desired_weapon` --
        this validates it against what the twin actually owns before ever
        calling equip(), same intent -> validate -> execute shape as every
        other intent. A request for a weapon not in inventory.weapons (a
        stale suggestion, or a controller bug) is silently ignored rather
        than trusted.
        """
        weapon_id = intent.desired_weapon
        inventory = state.twin.inventory
        if (
            weapon_id
            and weapon_id != inventory.equipped_weapon
            and weapon_id in inventory.weapons
        ):
            inventory.equip(weapon_id)
            state.emit("TWIN_WEAPON_SWITCH", weapon=weapon_id)

    def _close(self, state: GameState, opened: _Open, reason: str) -> None:
        twin = state.twin
        duration = (state.tick - opened.started_tick) / 60.0
        dealt = twin.damage_dealt - opened.dealt_at_start
        taken = twin.damage_taken - opened.taken_at_start
        kills = twin.kills - opened.kills_at_start
        if duration < MIN_OUTCOME_SECONDS and dealt == 0 and taken == 0:
            return
        kind = opened.intent.intent_type
        if kind in OFFENSIVE:
            success = dealt > 0 and taken <= max(dealt * 0.9, 8.0)
        elif kind == "RETREAT":
            success = taken <= 2.0
        else:
            success = taken <= 2.0
        state.emit(
            "TWIN_OUTCOME",
            intent=kind,
            target=opened.intent.target_id,
            success=success,
            damage_dealt=round(dealt, 1),
            damage_taken=round(taken, 1),
            kills=kills,
            duration=round(duration, 2),
            end_reason=reason,
            confidence=round(opened.intent.confidence, 3),
        )

    # --- per tick ------------------------------------------------------------------

    def apply(self, dt: float, state: GameState, combat: CombatSystem) -> None:
        twin = state.twin
        intent = twin.intent
        if twin.downed:
            twin.velocity = Vec2()
            return

        target = state.entity_by_id(intent.target_id)
        target_enemy = target if isinstance(target, Enemy) and target.active else None
        kind = intent.intent_type

        if kind in OFFENSIVE and target_enemy is not None:
            self._engage(dt, state, combat, target_enemy, hold_position=intent.position if kind in ("FLANK", "PROTECT", "INTERCEPT") else None)
        elif kind in OFFENSIVE:
            # Target vanished: drift back to the player until the next decision.
            self._move_to(dt, state, self._follow_point(state))
        elif kind in ("FOLLOW", "REPOSITION", "EXPLORE", "RETREAT", "HEAL"):
            goal = intent.position or self._follow_point(state)
            self._move_to(dt, state, goal)
            if kind == "RETREAT" and target_enemy is None:
                nearest = state.nearest_enemy(twin.position, 260)
                if nearest is not None and not twin.weapon.is_melee and twin.can_attack():
                    # Cover fire while backing off.
                    combat.process_twin_attack(state, nearest)
        else:
            self._move_to(dt, state, self._follow_point(state))

    # --- movement primitives ----------------------------------------------------------

    @staticmethod
    def _follow_point(state: GameState) -> Vec2:
        player = state.player
        return player.position - player.facing * 62 + player.facing.perpendicular() * 26

    def _move_to(self, dt: float, state: GameState, goal: Vec2) -> None:
        twin = state.twin
        goal = state.room.clamp(goal, twin.radius)
        diff = goal - twin.position
        dist = diff.length()
        if dist < 10:
            twin.velocity = Vec2()
            return
        speed = min(twin.speed * twin.slow_factor, dist / max(dt, 1e-3))
        twin.velocity = diff.normalized() * speed
        twin.face(diff)

    def _engage(self, dt: float, state: GameState, combat: CombatSystem, target: Enemy,
                hold_position: Vec2 | None) -> None:
        twin = state.twin
        weapon = twin.weapon
        to_target = target.position - twin.position
        dist = to_target.length()
        direction = to_target.normalized() if dist > 0 else twin.facing

        if weapon.is_melee:
            reach = weapon.range * 0.85 + target.hit_radius
            if hold_position is not None and (hold_position - twin.position).length() > 18 and dist > reach:
                self._move_to(dt, state, hold_position)
            elif dist > reach:
                self._move_to(dt, state, target.position - direction * (reach * 0.8))
            else:
                twin.velocity = Vec2()
            twin.face(direction)
            if dist <= weapon.range + target.hit_radius and twin.can_attack():
                combat.process_twin_attack(state, target)
            return

        hold = weapon.range * 0.55
        if hold_position is not None and (hold_position - twin.position).length() > 18:
            self._move_to(dt, state, hold_position)
        elif dist > hold + 40:
            self._move_to(dt, state, target.position - direction * hold)
        elif dist < hold - 70:
            back = state.room.clamp(twin.position - direction * 80, twin.radius)
            self._move_to(dt, state, back)
        else:
            # Slow strafe keeps a ranged twin from being a static turret.
            side = 1 if (state.tick // 90) % 2 == 0 else -1
            twin.velocity = direction.perpendicular() * (twin.speed * 0.25 * side)
        twin.face(direction)
        if dist <= weapon.range and twin.can_attack():
            combat.process_twin_attack(state, target)

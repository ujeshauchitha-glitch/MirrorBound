"""Combat system: attacks, abilities, and the single damage pipeline.

Flow for every hit, regardless of who threw it:

    INPUT/INTENT -> validation (cooldown, resource, state)
                 -> hit detection (arc / cone / radius / projectile)
                 -> damage (`damage_enemy` / `damage_player` / `damage_twin`)
                 -> effects (knockback, slow, stagger, death, loot, xp)
                 -> events on the bus (telemetry, client VFX)
"""

from __future__ import annotations

import math

from mirrorbound.game.combat.abilities import AbilityDef, AbilityType
from mirrorbound.game.combat.hitbox import Hitbox, HitboxShape
from mirrorbound.game.combat.mitigation import apply_reduction
from mirrorbound.game.combat.weapons import ProjectileSpec, WeaponDef
from mirrorbound.game.core.events import EventBus
from mirrorbound.game.core.rng import DeterministicRNG
from mirrorbound.game.entities.enemy import Enemy
from mirrorbound.game.entities.entity import Entity, Vec2
from mirrorbound.game.entities.projectile import Projectile
from mirrorbound.game.loot import LootSystem
from mirrorbound.game.state import GameState


# The companion fights with the same weapons but hits softer: the player must
# stay the one who wins fights, or the twin's help turns into watching.
TWIN_DAMAGE_MULT = 0.6


class CombatSystem:
    """Handles all combat operations."""

    def __init__(self, bus: EventBus, rng: DeterministicRNG):
        self.bus = bus
        self.rng = rng.spawn("combat")
        self.loot = LootSystem(rng)

    # ------------------------------------------------------------------ player

    def process_player_attack(self, state: GameState) -> bool:
        """Basic attack in the player's facing direction. Returns True if it fired."""
        player = state.player
        weapon = player.weapon
        if not player.can_attack():
            return False
        if weapon.resource_cost > 0 and player.mana < weapon.resource_cost:
            state.emit("ACTION_REJECTED", actor=player.id, action="ATTACK", reason="mana")
            return False

        combo_mult = player.start_attack(weapon)
        if weapon.resource_cost > 0:
            player.mana -= weapon.resource_cost
        facing = player.facing
        hits: list[Enemy] = []

        if weapon.is_melee:
            hits = self._melee_sweep(state, player, weapon, facing, combo_mult, attacker_id=player.id)
        else:
            self._fire_projectiles(state, player, weapon.projectile, facing, weapon.damage * combo_mult
                                   * player.weapon_damage_multiplier(), weapon.knockback, weapon.tags,
                                   source=weapon.id)

        token = f"{weapon.family.upper()}_{'STRIKE' if weapon.is_melee else 'SHOT'}"
        if weapon.is_melee and player.combo_step == len(weapon.combo_chain) and len(weapon.combo_chain) > 1:
            token = f"{weapon.family.upper()}_FINISHER"
        state.emit(
            "PLAYER_ATTACKED",
            action_token=token,
            tags=weapon.get_tags(),
            weapon=weapon.id,
            position=player.position.to_dict(),
            facing=facing.to_dict(),
            comboStep=player.combo_step,
            targets=[e.id for e in hits],
            hitCount=len(hits),
            nearestEnemyDistance=self._nearest_enemy_distance(state, player.position),
        )
        return True

    def _melee_sweep(self, state: GameState, attacker: Entity, weapon: WeaponDef, facing: Vec2,
                     mult: float, attacker_id: str) -> list[Enemy]:
        hitbox = Hitbox(
            shape=HitboxShape.ARC,
            position=attacker.position,
            size=weapon.range,
            direction=facing.angle(),
            arc_angle=weapon.arc_angle,
        )
        # hit_radius, not radius: how big a target it is, not how much room it
        # takes up. See EnemyDef.hit_radius.
        hits = [e for e in state.get_active_enemies()
                if hitbox.overlaps_circle(e.position, e.hit_radius)]
        is_player = attacker_id == state.player.id
        dmg_mult = state.player.weapon_damage_multiplier() if is_player else 1.0
        crit_bonus = state.player.crit_chance_bonus() if is_player else 0.0
        knock_mult = state.player.mods.knockback_mult if is_player else 1.0
        for enemy in hits:
            crit = self.rng.chance(weapon.crit_chance + crit_bonus)
            damage = weapon.damage * mult * dmg_mult * (weapon.crit_multiplier if crit else 1.0)
            direction = (enemy.position - attacker.position).normalized()
            self.damage_enemy(state, enemy, damage, attacker_id, weapon.get_tags(), direction,
                              weapon.knockback * knock_mult * (1.4 if mult > 1.2 else 1.0), weapon.id, crit)
        return hits

    def _fire_projectiles(self, state: GameState, owner: Entity, spec: ProjectileSpec | None, facing: Vec2,
                          damage: float, knockback: float, tags: tuple[str, ...], source: str) -> list[Projectile]:
        if spec is None:
            return []
        out = []
        base_angle = facing.angle()
        for i in range(spec.count):
            spread = (i - (spec.count - 1) / 2) * spec.spread
            direction = Vec2.from_angle(base_angle + spread)
            projectile = Projectile(
                id=state.ids.next("projectile"),
                owner_id=owner.id,
                kind=spec.kind,
                position=owner.position + direction * (owner.radius + spec.radius + 2),
                velocity=direction * spec.speed,
                facing=direction,
                damage=damage,
                speed=spec.speed,
                radius=spec.radius,
                lifetime=spec.lifetime,
                pierce=spec.pierce,
                aoe_radius=spec.aoe_radius,
                knockback=knockback,
                slow=spec.slow,
                slow_duration=spec.slow_duration,
                tags=tuple(tags),
                source=source,
            )
            state.projectiles.append(projectile)
            out.append(projectile)
        return out

    def process_ability(self, state: GameState, slot: int) -> bool:
        player = state.player
        ability = player.ability_in_slot(slot)
        if ability is None:
            return False
        ok, reason = player.can_use_ability(ability)
        if not ok:
            state.emit("ACTION_REJECTED", actor=player.id, action=f"ABILITY_{slot}", ability=ability.id, reason=reason)
            return False

        player.start_ability(ability)
        state.stats.abilities_cast += 1
        facing = player.facing
        hits: list[str] = []
        extra: dict = {}

        if ability.type is AbilityType.PROJECTILE:
            spec = ability.projectile
            dmg = ability.damage * player.spell_damage_multiplier()
            self._fire_projectiles(state, player, spec, facing, dmg, 90, ability.tags, source=ability.id)
        elif ability.type is AbilityType.CONE:
            hitbox = Hitbox(shape=HitboxShape.ARC, position=player.position, size=ability.area,
                            direction=facing.angle(), arc_angle=ability.cone_angle)
            for enemy in state.get_active_enemies():
                if hitbox.overlaps_circle(enemy.position, enemy.hit_radius):
                    dist = (enemy.position - player.position).length()
                    falloff = 1.0 - 0.35 * min(1.0, dist / max(ability.area, 1))
                    dmg = ability.damage * player.spell_damage_multiplier() * falloff
                    direction = (enemy.position - player.position).normalized()
                    self.damage_enemy(state, enemy, dmg, player.id, list(ability.tags), direction, 160, ability.id)
                    if "BURN" in ability.effect_tags:
                        enemy.apply_status("burn", 1.5)
                    hits.append(enemy.id)
        elif ability.type is AbilityType.DASH:
            direction = player.last_move_dir if not player.velocity.is_zero() else player.facing
            invuln = ability.duration + player.mods.dash_invuln_bonus
            start = player.position.copy()
            player.begin_dash(direction, ability.effect_value, ability.duration, invuln)
            dodged = [
                e.id for e in state.get_active_enemies()
                if e.is_winding_up and e.target_id == player.id and (e.position - start).length() < e.enemy_def.attack_range * 2.5
            ]
            extra = {"distance": ability.effect_value, "dodged": dodged}
            state.emit("PLAYER_DASHED", action_token="DASH", tags=["MOBILITY"], distance=ability.effect_value,
                       position=start.to_dict(), direction=direction.normalized().to_dict())
            if dodged:
                state.emit("PLAYER_DODGED", tags=["MOBILITY", "DEFENSIVE"], position=start.to_dict(),
                           dodged=dodged, distance=ability.effect_value)
        elif ability.type is AbilityType.NOVA:
            for enemy in state.get_active_enemies():
                dist = (enemy.position - player.position).length()
                if dist <= ability.area + enemy.hit_radius:
                    dmg = ability.damage * player.spell_damage_multiplier()
                    direction = (enemy.position - player.position).normalized()
                    self.damage_enemy(state, enemy, dmg, player.id, list(ability.tags), direction, 120, ability.id)
                    if "SLOW" in ability.effect_tags and enemy.active:
                        enemy.apply_status("slow", ability.duration, slow_factor=ability.effect_value)
                    hits.append(enemy.id)
        elif ability.type is AbilityType.HEAL:
            if ability.cast_time > 0:
                # A channel: the heal lands when the channel finishes, and only
                # if nothing interrupts it. The session completes it.
                player.begin_channel(ability.id, ability.cast_time)
                extra = {"channel": ability.cast_time}
            else:
                healed = player.heal(ability.effect_value)
                state.emit("PLAYER_HEALED", amount=healed, remaining=player.health, source=ability.id)
        elif ability.type is AbilityType.SHIELD:
            player.apply_status("shield", ability.duration)
            extra = {"duration": ability.duration}

        state.emit(
            "PLAYER_ABILITY_CAST",
            ability=ability.id.upper(),
            ability_id=ability.id,
            tags=list(ability.tags),
            slot=slot,
            position=player.position.to_dict(),
            facing=facing.to_dict(),
            targets=hits,
            hitCount=len(hits),
            nearestEnemyDistance=self._nearest_enemy_distance(state, player.position),
            **extra,
        )
        return True

    # -------------------------------------------------------------------- twin

    def process_twin_attack(self, state: GameState, target: Enemy) -> bool:
        twin = state.twin
        if not twin.can_attack() or not target.active:
            return False
        weapon = twin.weapon
        if weapon.resource_cost > 0 and twin.mana < weapon.resource_cost:
            return False
        direction = (target.position - twin.position).normalized()
        twin.face(direction)
        twin.start_attack()
        if weapon.resource_cost > 0:
            twin.mana -= weapon.resource_cost
        hits: list[str] = []
        if weapon.is_melee:
            hits = [e.id for e in self._melee_sweep(state, twin, weapon, direction, TWIN_DAMAGE_MULT, attacker_id=twin.id)]
        else:
            self._fire_projectiles(state, twin, weapon.projectile, direction, weapon.damage * TWIN_DAMAGE_MULT,
                                   weapon.knockback, weapon.tags, source=weapon.id)
        state.emit("TWIN_ATTACKED", target=target.id, weapon=weapon.id, tags=weapon.get_tags(),
                   position=twin.position.to_dict(), hitCount=len(hits),
                   distance=(target.position - twin.position).length())
        return True

    # ------------------------------------------------------------------ enemies

    def process_enemy_attack(self, state: GameState, enemy: Enemy, target: Entity,
                             ranged: bool | None = None) -> bool:
        """The wind-up finished: land the hit or fire the projectile.

        `ranged` forces the mode; by default an enemy with a projectile shoots
        unless the target is practically touching it.
        """
        edef = enemy.enemy_def
        to_target = target.position - enemy.position
        dist = to_target.length()
        direction = to_target.normalized() if dist > 0 else enemy.facing
        enemy.face(direction)
        enemy.attack_timer = edef.attack_cooldown
        landed = False

        if ranged is None:
            ranged = edef.projectile is not None and dist > edef.attack_range * 0.35
        if ranged and edef.projectile is not None:
            # Lead the shot slightly toward where the target is heading.
            lead = target.velocity * min(0.35, dist / max(edef.projectile.speed, 1))
            aim = ((target.position + lead) - enemy.position).normalized()
            self._fire_projectiles(state, enemy, edef.projectile, aim, edef.damage, edef.knockback,
                                   edef.tags, source=edef.id)
            landed = True
        else:
            reach = edef.attack_range + target.radius + 6
            if dist <= reach:
                landed = self._damage_ally(state, target, edef.damage, enemy.id, direction, edef.knockback) > 0

        state.emit("ENEMY_ATTACKED", enemy_id=enemy.id, enemy_type=edef.id, target=target.id, hit=landed,
                   position=enemy.position.to_dict(), ranged=edef.projectile is not None)
        return landed

    # ------------------------------------------------------------- damage core

    def _damage_ally(self, state: GameState, target: Entity, amount: float, attacker_id: str,
                     direction: Vec2, knockback: float) -> float:
        if target.id == state.player.id:
            return self.damage_player(state, amount, attacker_id, direction, knockback)
        if target.id == state.twin.id:
            return self.damage_twin(state, amount, attacker_id, direction, knockback)
        return 0.0

    def damage_enemy(self, state: GameState, enemy: Enemy, amount: float, attacker_id: str, tags: list[str],
                     direction: Vec2, knockback: float, source: str, crit: bool = False) -> float:
        if not enemy.active:
            return 0.0
        actual = enemy.take_hit(amount, attacker_id)
        if actual <= 0:
            return 0.0
        resist = 1.0 - enemy.enemy_def.knockback_resist
        if knockback > 0 and resist > 0:
            enemy.knockback = enemy.knockback + direction * (knockback * resist)
        state.stats.damage_dealt += actual
        if attacker_id == state.twin.id:
            state.twin.damage_dealt += actual
        if attacker_id == state.player.id and state.player.target_id != enemy.id:
            previous = state.player.target_id
            state.player.target_id = enemy.id
            state.emit("TARGET_CHANGE", actor=state.player.id, previous=previous, target=enemy.id,
                       target_type=enemy.enemy_def.id, position=state.player.position.to_dict())
        state.emit(
            "DAMAGE_DEALT",
            attacker=attacker_id,
            target=enemy.id,
            target_type=enemy.enemy_def.id,
            damage=round(actual, 1),
            remaining=round(enemy.health, 1),
            position=enemy.position.to_dict(),
            crit=crit,
            source=source,
            tags=list(tags),
        )
        if not enemy.active:
            self.on_enemy_killed(state, enemy, attacker_id)
        return actual

    def on_enemy_killed(self, state: GameState, enemy: Enemy, killer_id: str) -> None:
        edef = enemy.enemy_def
        state.stats.enemies_killed += 1
        if killer_id == state.twin.id:
            state.twin.kills += 1
        else:
            state.player.kills += 1
        # XP is shared: the twin's kills still grow the player.
        levels = state.player.add_xp(edef.xp_reward)
        self.loot.drop_for(state, enemy)
        state.emit(
            "ENEMY_KILLED",
            enemy_id=enemy.id,
            enemy_type=edef.id,
            role=edef.role,
            elite=edef.elite,
            boss=edef.boss,
            xp_reward=edef.xp_reward,
            killer=killer_id,
            position=enemy.position.to_dict(),
            room_id=state.room.id,
        )
        for reward in levels:
            state.emit("LEVEL_UP", level=reward["level"], skillPoints=state.player.skill_points,
                       maxHealth=state.player.max_health, position=state.player.position.to_dict())
        if edef.boss:
            state.emit("BOSS_DEFEATED", enemy_id=enemy.id, position=enemy.position.to_dict())

    def damage_player(self, state: GameState, amount: float, attacker_id: str, direction: Vec2,
                      knockback: float) -> float:
        player = state.player
        if player.state == "dead" or player.invulnerable_for > 0:
            if player.invulnerable_for > 0 and player.state == "dash":
                state.emit("PLAYER_DODGED", tags=["MOBILITY", "DEFENSIVE"], position=player.position.to_dict(),
                           dodged=[attacker_id], distance=0.0)
            return 0.0
        reduced, mitigations = apply_reduction(state, player.id, amount)
        actual = player.take_hit(reduced)
        if actual <= 0:
            return 0.0
        if knockback > 0:
            player.knockback = player.knockback + direction * knockback * 0.6
        # A hit breaks a channelled cast. The mana is already spent, so this is
        # the real cost of trying to heal while something can still reach you.
        interrupted = player.interrupt_channel()
        if interrupted:
            state.emit("ABILITY_INTERRUPTED", actor=player.id, ability=interrupted, by=attacker_id,
                       position=player.position.to_dict())
        state.stats.damage_taken += actual
        attacker = state.entity_by_id(attacker_id)
        state.emit(
            "DAMAGE_TAKEN",
            actor=player.id,
            attacker=attacker_id,
            attacker_type=attacker.enemy_def.id if isinstance(attacker, Enemy) else "unknown",
            damage=round(actual, 1),
            remaining=round(player.health, 1),
            position=player.position.to_dict(),
            mitigatedBy=mitigations,
        )
        if player.state == "dead":
            state.phase = "dead"
            state.emit("PLAYER_DIED", position=player.position.to_dict(), killer=attacker_id,
                       room_id=state.room.id, tags=["HIGH_RISK"])
        return actual

    def damage_twin(self, state: GameState, amount: float, attacker_id: str, direction: Vec2,
                    knockback: float) -> float:
        twin = state.twin
        if twin.downed or twin.invulnerable_for > 0:
            return 0.0
        reduced, _ = apply_reduction(state, twin.id, amount)
        actual = twin.take_hit(reduced)
        if actual <= 0:
            return 0.0
        if knockback > 0:
            twin.knockback = twin.knockback + direction * knockback * 0.6
        state.emit("TWIN_DAMAGED", actor=twin.id, attacker=attacker_id, damage=round(actual, 1),
                   remaining=round(twin.health, 1), position=twin.position.to_dict(),
                   intent=twin.intent.intent_type)
        if twin.downed:
            state.emit("TWIN_DOWNED", position=twin.position.to_dict(), attacker=attacker_id,
                       intent=twin.intent.intent_type)
        return actual

    def projectile_hit_enemy(self, state: GameState, projectile: Projectile, enemy: Enemy) -> None:
        direction = projectile.velocity.normalized()
        if projectile.aoe_radius > 0:
            centre = enemy.position
            for other in state.get_active_enemies():
                d = (other.position - centre).length()
                if d <= projectile.aoe_radius + other.hit_radius:
                    falloff = 1.0 if other.id == enemy.id else max(0.45, 1.0 - d / (projectile.aoe_radius + other.hit_radius))
                    self.damage_enemy(state, other, projectile.damage * falloff, projectile.owner_id,
                                      list(projectile.tags), (other.position - centre).normalized() if other.id != enemy.id else direction,
                                      projectile.knockback, projectile.source)
                    if projectile.slow > 0 and other.active:
                        other.apply_status("slow", projectile.slow_duration, slow_factor=projectile.slow)
                    projectile.hit_ids.add(other.id)
        else:
            self.damage_enemy(state, enemy, projectile.damage, projectile.owner_id, list(projectile.tags),
                              direction, projectile.knockback, projectile.source)
            if projectile.slow > 0 and enemy.active:
                enemy.apply_status("slow", projectile.slow_duration, slow_factor=projectile.slow)
        state.emit("PROJECTILE_HIT", projectile=projectile.id, kind=projectile.kind, target=enemy.id,
                   position=enemy.position.to_dict(), aoe=projectile.aoe_radius)
        projectile.hit_target(enemy.id)

    def projectile_hit_ally(self, state: GameState, projectile: Projectile, target: Entity) -> None:
        direction = projectile.velocity.normalized()
        dealt = self._damage_ally(state, target, projectile.damage, projectile.owner_id, direction, projectile.knockback)
        state.emit("PROJECTILE_HIT", projectile=projectile.id, kind=projectile.kind, target=target.id,
                   position=target.position.to_dict(), aoe=0, dealt=dealt)
        projectile.hit_target(target.id)

    # ------------------------------------------------------------------- misc

    @staticmethod
    def _nearest_enemy_distance(state: GameState, pos: Vec2) -> float | None:
        enemy = state.nearest_enemy(pos)
        return round((enemy.position - pos).length(), 1) if enemy else None

    def update(self, dt: float, state: GameState) -> None:
        """Per-tick combat bookkeeping (entities own their cooldown timers)."""
        for enemy in state.get_active_enemies():
            enemy.tick_status(dt)
            if enemy.stagger > 0:
                enemy.stagger -= dt
            if "burn" in enemy.status_effects:
                # Burn ticks 4 dps, attributed to the player.
                tick_damage = 4.0 * dt
                if enemy.health > tick_damage:
                    enemy.health -= tick_damage
                    state.stats.damage_dealt += tick_damage


__all__ = ["CombatSystem", "math"]

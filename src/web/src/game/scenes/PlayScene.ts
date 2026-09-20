/**
 * The play scene: renders whatever the server says the world is.
 *
 * Responsibilities: connect, sample input and send it, keep entity views in
 * sync with snapshots, turn server events into VFX/audio/reactions, drive the
 * camera, and draw the optional AI debug overlay. It decides nothing about
 * gameplay.
 */

import Phaser from 'phaser';

import { audio } from '../audio/AudioManager';
import { CAMERA, DEPTH, PALETTE, RENDER_SCALE } from '../constants';
import type { CommandMessage, EnemySnap, GameSnapshot, RoomFull, ServerEvent, Vec2 } from '../contracts';
import { isRoomFull } from '../contracts';
import { Vfx } from '../effects/Vfx';
import { EnemyView } from '../entities/EnemyView';
import { Hatch } from '../entities/Hatch';
import { PickupView } from '../entities/PickupView';
import { PlayerView } from '../entities/PlayerView';
import { ProjectileView } from '../entities/ProjectileView';
import { TwinView } from '../entities/TwinView';
import { WeaponOverlay } from '../entities/WeaponOverlay';
import { eventBus } from '../EventBus';
import { KeyboardIntentSource } from '../input/KeyboardIntentSource';
import { EnemyAtlasLoader } from './EnemyAtlasLoader';
import { HudScene } from './HudScene';
import { WebSocketClient } from '../network/WebSocketClient';
import type { Intent, PlayerSnapshot } from '../types';
import { Ambient } from '../world/Ambient';
import { TextureFactory } from '../world/TextureFactory';
import { WorldRenderer } from '../world/WorldRenderer';
import { getSettings, type Settings } from '../../ui/settings';

/** Death-burst colour per sprite. The seven the server does not send yet are
 *  here so adding them costs no client change. */
const ENEMY_COLOUR: Record<string, number> = {
  skeleton: 0xdcd4c4, archer: 0xffd27a, hound: 0x6a5a8a, slime: 0x63c26d, mirror: 0xd62e6c,
  acolyte: 0xb48cff, brute: 0xc07a4a, scarab: 0x8fb36a, shardling: 0x9fe3ff,
  spitter: 0x9ad06a, sprout: 0x7fbf5a, warden: 0xf0c060,
};

export class PlayScene extends Phaser.Scene {
  static readonly KEY = 'play';

  #ws!: WebSocketClient;
  #textures!: TextureFactory;
  #world!: WorldRenderer;
  #ambient!: Ambient;
  #vfx!: Vfx;
  #source!: KeyboardIntentSource;
  #weapon!: WeaponOverlay;
  #enemyAtlases!: EnemyAtlasLoader;
  #player: PlayerView | null = null;
  #twin: TwinView | null = null;
  #enemies = new Map<string, EnemyView>();
  #projectiles = new Map<string, ProjectileView>();
  #pickups = new Map<string, PickupView>();
  #snapshot: GameSnapshot | null = null;
  #room: RoomFull | null = null;
  #settings: Settings = getSettings();
  #modalOpen = false;
  #debug!: Phaser.GameObjects.Graphics;
  #vignette!: Phaser.GameObjects.Image;
  #teardown: Array<() => void> = [];
  #lastUiSnapshot: PlayerSnapshot | null = null;
  #cameraBound = false;
  /** Highest event tick already turned into feedback; see `#onEvents`. */
  #lastEventTick = -1;

  constructor() {
    super(PlayScene.KEY);
  }

  create(): void {
    console.info(`[mirrorbound] play scene created at ${Math.round(performance.now())}ms`);
    this.#textures = new TextureFactory(this);
    this.#textures.ensureCommon();
    this.#world = new WorldRenderer(this, this.#textures, this.#settings.quality);
    this.#ambient = new Ambient(this, this.#settings.quality);
    this.#vfx = new Vfx(this, this.#settings);
    this.#weapon = new WeaponOverlay(this);
    // When a family's sheets land, every enemy of that family standing in with
    // the painted texture swaps to the real art in place.
    this.#enemyAtlases = new EnemyAtlasLoader(this, (sprites) => {
      const arrived = new Set<string>(sprites);
      for (const view of this.#enemies.values()) {
        if (view.painted && arrived.has(view.snap.sprite)) view.adoptArt();
      }
    });
    this.#debug = this.add.graphics().setDepth(DEPTH.debug);
    this.#vignette = this.add.image(0, 0, 'fx:vignette').setDepth(DEPTH.vignette).setAlpha(0.8);

    this.#source = new KeyboardIntentSource(this.input.keyboard!);
    this.cameras.main.setBackgroundColor(PALETTE.night);
    this.cameras.main.setZoom(RENDER_SCALE * this.#settings.zoom);

    // Audio can only start on a gesture; the first key or click unlocks it.
    const unlock = () => audio.unlock();
    this.input.on('pointerdown', unlock);
    this.input.keyboard?.on('keydown', unlock);
    audio.applySettings(this.#settings);

    this.#ws = new WebSocketClient();
    this.#ws.connect();

    this.#teardown.push(
      eventBus.on('game:snapshot', (snap) => this.#onSnapshot(snap)),
      eventBus.on('game:events', (events) => this.#onEvents(events)),
      eventBus.on('ui:command', (cmd) => this.#onCommand(cmd)),
      eventBus.on('ui:modal', ({ open }) => {
        this.#modalOpen = open;
        this.#source.muted = open;
      }),
      eventBus.on('ui:settings', (settings) => this.#applySettings(settings)),
      eventBus.on('game:toggle-fullscreen', () => {
        if (this.scale.isFullscreen) this.scale.stopFullscreen();
        else this.scale.startFullscreen();
      }),
    );
    for (const event of [Phaser.Scale.Events.ENTER_FULLSCREEN, Phaser.Scale.Events.LEAVE_FULLSCREEN]) {
      this.scale.on(event, () => eventBus.emit('game:fullscreen', { active: this.scale.isFullscreen }));
    }
    this.events.once(Phaser.Scenes.Events.SHUTDOWN, () => this.#dispose());

    // The interface is its own scene on its own camera. It has to be, because
    // this camera is zoomed by RENDER_SCALE to supersample the artwork, and a
    // scroll-factor-zero object under a zoomed camera still has to be placed in
    // that camera's transformed space. A second scene gets an untouched one, so
    // every number in `HUD_ART` is a plain canvas pixel.
    //
    // Launched here rather than listed as active, so it is built after the
    // world it draws over.
    if (!this.scene.isActive(HudScene.KEY)) this.scene.launch(HudScene.KEY);

    eventBus.emit('game:ready', { scene: PlayScene.KEY });
  }

  // --- frame -----------------------------------------------------------------------

  /**
   * The intent, pointed at the cursor.
   *
   * The pointer is in screen pixels; the player is in world units under a
   * camera that scrolls and is zoomed by `RENDER_SCALE`. `positionToCamera`
   * undoes both, and the difference is which way the player is looking.
   *
   * Zero when the pointer has never moved over the canvas or is sitting on the
   * player, and the server falls back to facing the way you are walking -- so
   * a keyboard-only player is unaffected by any of this.
   */
  #aimed(intent: Intent): Intent {
    const player = this.#player;
    const pointer = this.input.activePointer;
    if (!player) return intent;

    const world = pointer.positionToCamera(this.cameras.main) as Phaser.Math.Vector2;
    const dx = world.x - player.x;
    const dy = world.y - player.y;
    const length = Math.hypot(dx, dy);
    // A few pixels of deadzone, so a cursor resting on the character does not
    // spin the sprite as it drifts under it.
    if (length < 8) return intent;
    return { ...intent, aimX: dx / length, aimY: dy / length };
  }

  override update(time: number, deltaMs: number): void {
    const dt = Math.min(deltaMs, 50) / 1000;
    const intent = this.#source.sample();
    const snap = this.#snapshot;
    const playing = snap !== null && snap.phase === 'playing' && !snap.paused && !this.#modalOpen;

    if (this.#player) {
      if (playing) this.#player.predict(dt, intent);
      this.#player.update(dt);
      // Anyone standing in a village turns to watch you walk past.
      this.#world.facePeople({ x: this.#player.x, y: this.#player.y });
      this.#weapon.place({ x: this.#player.x, y: this.#player.y }, this.#player.facingVec);
      const ui = this.#player.snapshot();
      if (!this.#lastUiSnapshot || ui.state !== this.#lastUiSnapshot.state || ui.facing !== this.#lastUiSnapshot.facing) {
        this.#lastUiSnapshot = ui;
        eventBus.emit('player:changed', ui);
      }
    }
    this.#ws.sendInput(
      playing ? this.#aimed(intent)
        : { moveX: 0, moveY: 0, attack: false, run: false, ability: null, aimX: 0, aimY: 0 },
      time,
    );

    this.#twin?.update(dt);
    for (const e of this.#enemies.values()) e.update(dt);
    for (const p of this.#projectiles.values()) p.update(dt);
    for (const p of this.#pickups.values()) p.update(dt);
    this.#ambient.update(dt, this.#player ? { x: this.#player.x, y: this.#player.y } : null);

    const cam = this.cameras.main;
    this.#vignette.setPosition(cam.midPoint.x, cam.midPoint.y);
    this.#vignette.setScale((cam.displayWidth / this.#vignette.width) * 1.02, (cam.displayHeight / this.#vignette.height) * 1.02);
    this.#drawDebug();
  }

  // --- snapshots ------------------------------------------------------------------------

  #onSnapshot(snap: GameSnapshot): void {
    const prev = this.#snapshot;
    this.#snapshot = snap;

    if (isRoomFull(snap.room)) {
      if (!this.#room || this.#room.index !== snap.room.index || this.#room.seed !== snap.room.seed) {
        this.#enterRoom(snap.room);
      } else {
        this.#room = snap.room;
        this.#world.updateDoors(snap.room.doors);
      }
    } else if (this.#room) {
      this.#room.cleared = snap.room.cleared;
      this.#room.doors = snap.room.doors;
      this.#world.updateDoors(snap.room.doors);
    }

    if (!this.#player) {
      this.#player = new PlayerView(this, snap.player.position);
      this.#player.setRoom(this.#room);
      this.#bindCamera();
    }
    this.#player.applySnapshot(snap.player, snap.player.stats?.speed);
    if (snap.player.currentWeapon !== this.#weapon.equipped) {
      const weapon = snap.player.weapon;
      this.#weapon.equip(weapon ? weapon.animation : this.#animationFor(snap.player.currentWeapon));
    }

    // A dormant twin is not in the world yet. The server still simulates an
    // entity for it -- it has a position and a health pool from the first tick
    // -- but nothing has found it, so nothing may draw it. Without this it
    // trails the player from the opening village and the rescue two rooms into
    // the crypt is a scene about someone already standing there.
    if (snap.twin.dormant) {
      this.#twin?.destroy();
      this.#twin = null;
    } else {
      // Created at the twin's own position, not the last-known one, so waking
      // it does not play a slide in from wherever the view was last left.
      if (!this.#twin) this.#twin = new TwinView(this, snap.twin.position);
      this.#twin.showThoughts = this.#settings.showTwinThoughts;
      this.#twin.applySnapshot(snap.twin);
    }

    this.#syncEnemies(snap.enemies);
    this.#syncProjectiles(snap);
    this.#syncPickups(snap);

    if (prev?.paused !== snap.paused) audio.setPaused(snap.paused);
  }

  #animationFor(weaponId: string): string {
    if (weaponId.includes('bow')) return 'bow';
    if (weaponId.includes('ember') || weaponId.includes('fire')) return 'fireStaff';
    if (weaponId.includes('frost') || weaponId.includes('ice')) return 'iceStaff';
    return 'sword';
  }

  #enterRoom(room: RoomFull): void {
    this.#room = room;
    // Ask for this room's enemy art the moment the room is known, which is one
    // or more snapshots before its enemies are drawn. A village names none, so
    // a safe room fetches nothing.
    this.#enemyAtlases.request(room.enemySprites ?? []);
    for (const e of this.#enemies.values()) e.destroy();
    this.#enemies.clear();
    for (const p of this.#projectiles.values()) p.destroy();
    this.#projectiles.clear();
    for (const p of this.#pickups.values()) p.destroy();
    this.#pickups.clear();
    this.#world.build(room);
    this.#ambient.build(room);
    this.#player?.setRoom(room);
    const cam = this.cameras.main;
    cam.setBounds(0, 0, room.width, room.height);
    if (!this.#cameraBound) this.#vfx.fadeIn(700);
  }

  #bindCamera(): void {
    if (!this.#player || this.#cameraBound) return;
    this.#cameraBound = true;
    const cam = this.cameras.main;
    cam.startFollow(this.#player.sprite, false, CAMERA.lerp, CAMERA.lerp, 0, 18);
    cam.setDeadzone(CAMERA.deadzone.width, CAMERA.deadzone.height);
    if (this.#room) cam.setBounds(0, 0, this.#room.width, this.#room.height);
    cam.centerOn(this.#player.x, this.#player.y);
  }

  #syncEnemies(enemies: EnemySnap[]): void {
    const seen = new Set<string>();
    /**
     * Backstop for the room's own sprite list.
     *
     * `RoomFull.enemySprites` is the spawn table's answer and arrives first, so
     * it is what usually triggers the fetch. Anything that turns up without
     * having been in it -- a summon, a future spawner, an older server with no
     * such field -- is asked for here instead. `request` de-duplicates, so a
     * family already loaded or in flight costs nothing.
     */
    let unloaded: string[] | null = null;
    for (const e of enemies) {
      seen.add(e.id);
      const view = this.#enemies.get(e.id);
      if (view) view.applySnapshot(e);
      else this.#enemies.set(e.id, new EnemyView(this, e));
      if (this.#enemyAtlases.needs(e.sprite)) (unloaded ??= []).push(e.sprite);
    }
    if (unloaded) this.#enemyAtlases.request(unloaded);
    for (const [id, view] of this.#enemies) {
      if (!seen.has(id)) {
        // Killed (the ENEMY_KILLED event usually gets here first) or room changed.
        const pos = view.die();
        this.#vfx.deathBurst(pos, ENEMY_COLOUR[view.snap.sprite] ?? 0xaaaaaa, view.snap.boss);
        this.#enemies.delete(id);
      }
    }
  }

  #syncProjectiles(snap: GameSnapshot): void {
    const seen = new Set<string>();
    for (const p of snap.projectiles) {
      seen.add(p.id);
      const view = this.#projectiles.get(p.id);
      if (view) view.applySnapshot(p);
      else this.#projectiles.set(p.id, new ProjectileView(this, p, this.#settings.quality === 'high'));
    }
    for (const [id, view] of this.#projectiles) {
      if (!seen.has(id)) {
        view.destroy();
        this.#projectiles.delete(id);
      }
    }
  }

  #syncPickups(snap: GameSnapshot): void {
    const seen = new Set<string>();
    for (const p of snap.pickups) {
      seen.add(p.id);
      const view = this.#pickups.get(p.id);
      if (view) view.applySnapshot(p);
      else this.#pickups.set(p.id, new PickupView(this, p));
    }
    for (const [id, view] of this.#pickups) {
      if (!seen.has(id)) {
        view.collect();
        this.#pickups.delete(id);
      }
    }
  }

  // --- events ---------------------------------------------------------------------------

  /**
   * Turn a batch of server events into feedback, exactly once each.
   *
   * Snapshots arrive twenty times a second and every one carries a tick, so an
   * event that turned up in two batches -- a resend, or a reconnect replaying
   * the tail -- would spawn its effect twice. Only ticks past the highest one
   * already handled are acted on; a batch that goes backwards means the server
   * restarted and the watermark is reset.
   */
  /**
   * Crack the companion open into the Mirror.
   *
   * Sixteen frames across two sheets, and the whole point of it is the size
   * change: the thing that has been following you all game becomes the thing
   * that is two and a half times your height. A boss that simply appeared at
   * full size would be a boss you never saw arrive.
   *
   * The real Mirror is hidden for the duration and shown on the last frame, so
   * what you watch is one creature becoming another rather than a cutscene
   * playing next to a boss that was already standing there.
   */
  #playHatch(at: Vec2): void {
    const boss = [...this.#enemies.values()].find((view) => view.snap.boss);
    boss?.sprite.setVisible(false);
    this.cameras.main.shake(260, 0.006);
    new Hatch(this, at.x, at.y, () => {
      boss?.sprite.setVisible(true);
    });
  }

  #onEvents(events: ServerEvent[]): void {
    if (events.length === 0) return;
    let batchHigh = events[0]!.tick;
    for (const e of events) batchHigh = Math.max(batchHigh, e.tick);
    // A batch entirely behind the watermark is the server having restarted.
    if (batchHigh < this.#lastEventTick) this.#lastEventTick = -1;
    for (const e of events) {
      if (e.tick <= this.#lastEventTick) continue;
      this.#onEvent(e);
    }
    this.#lastEventTick = Math.max(this.#lastEventTick, batchHigh);
  }

  #pos(e: ServerEvent, key = 'position'): Vec2 {
    const p = e.data[key] as Vec2 | undefined;
    return p ?? (this.#player ? { x: this.#player.x, y: this.#player.y } : { x: 0, y: 0 });
  }

  #onEvent(e: ServerEvent): void {
    const player = this.#player;
    switch (e.type) {
      // The twin becomes the boss, once, on the threshold of the Sanctum.
      //
      // The Mirror is drawn in the room already -- the server spawned it with
      // everything else -- so the hatch is played *over* it and the boss is
      // hidden until the shell opens. Doing it the other way round, spawning
      // the Mirror when the cutscene ends, would mean the server and the
      // client disagreed about what was in the room for a second and a half.
      case 'TWIN_TAKEN': {
        this.#playHatch(this.#pos(e));
        break;
      }

      case 'PLAYER_ATTACKED': {
        if (!player) break;
        const facing = (e.data.facing as Vec2) ?? player.facingVec;
        const weapon = String(e.data.weapon ?? '');
        const finisher = String(e.data.action_token ?? '').endsWith('FINISHER');
        // A weapon that throws something plays the motion Logesh drew for the
        // shot rather than its melee bash. Read off what is equipped rather
        // than off `player.weapon`, which only rides on detail snapshots.
        const thrown = this.#weapon.equipped !== null && this.#weapon.equipped !== 'sword';
        this.#weapon.strike(Number(e.data.comboStep ?? 1), { x: player.x, y: player.y }, facing, thrown);
        if (weapon.includes('sword')) {
          this.#vfx.slash({ x: player.x, y: player.y }, facing, finisher ? PALETTE.pink : 0xffffff, finisher ? 1.35 : 1);
          audio.play(finisher ? 'slash_heavy' : 'slash', { pitch: 0.95 + Number(e.data.comboStep ?? 1) * 0.06 });
        } else if (weapon.includes('bow')) audio.play('arrow');
        else if (weapon.includes('ember')) audio.play('fire', { volume: 0.7 });
        else audio.play('ice', { volume: 0.7 });
        break;
      }
      case 'PLAYER_ABILITY_CAST': {
        if (!player) break;
        const facing = (e.data.facing as Vec2) ?? player.facingVec;
        const pos = { x: player.x, y: player.y };
        const abilityId = String(e.data.ability_id);
        // The weapon's own motion while the spell fires. Without one the staff
        // idles through the cast and the spell reads as arriving from nowhere.
        this.#weapon.castAbility(abilityId, pos, facing);
        switch (abilityId) {
          case 'arcane_bolt': this.#vfx.arcaneCast(pos, facing); audio.play('arcane'); break;
          case 'flame_burst': this.#vfx.flameCone(pos, facing); audio.play('fire'); break;
          case 'shadow_dash': this.#vfx.dash(pos, (e.data.direction as Vec2) ?? facing); audio.play('dash'); break;
          case 'binding_nova': this.#vfx.nova(pos, 150); audio.play('nova'); break;
          default: break;
        }
        break;
      }
      case 'DAMAGE_DEALT': {
        const pos = this.#pos(e);
        const target = String(e.data.target);
        this.#enemies.get(target)?.hit();
        const source = String(e.data.source ?? '');
        const colour = source.includes('ember') || source.includes('flame') ? PALETTE.ember
          : source.includes('frost') ? PALETTE.ice : source.includes('arcane') || source.includes('nova') ? PALETTE.arcane : 0xffffff;
        const crit = Boolean(e.data.crit);
        this.#vfx.hitSparks({ x: pos.x, y: pos.y }, colour, crit ? 14 : 7);
        this.#vfx.damageNumber(pos, Number(e.data.damage), crit,
          String(e.data.attacker).startsWith('twin') ? '#bfe6ff' : '#fff1c9');
        audio.play('hit', { volume: 0.6, pitch: crit ? 0.8 : 1 });
        break;
      }
      case 'DAMAGE_TAKEN':
        this.#vfx.hurtFlash();
        this.#vfx.damageNumber(this.#pos(e), Number(e.data.damage), false, '#ff8a8a');
        audio.play('hit_player');
        break;
      case 'ENEMY_KILLED': {
        const id = String(e.data.enemy_id);
        const view = this.#enemies.get(id);
        const pos = view ? view.die() : this.#pos(e);
        this.#enemies.delete(id);
        const boss = Boolean(e.data.boss);
        this.#vfx.deathBurst(pos, ENEMY_COLOUR[String(e.data.enemy_type)] ?? 0xaaaaaa, boss);
        audio.play(boss ? 'boss_death' : 'enemy_death');
        if (String(e.data.killer).startsWith('twin')) this.#twin?.onCelebrate();
        break;
      }
      case 'PROJECTILE_HIT':
        this.#vfx.impact(this.#pos(e), String(e.data.kind));
        break;
      case 'PROJECTILE_EXPIRED':
        this.#vfx.hitSparks(this.#pos(e), 0xcccccc, 3);
        break;
      case 'ITEM_PICKUP': {
        const kind = String(e.data.kind);
        const colour = kind === 'essence' ? 0xc05bff : kind === 'shards' ? 0x9fe3ff : kind.includes('potion') ? 0xe04a5a : PALETTE.gold;
        this.#vfx.pickup(this.#pos(e), colour);
        audio.play(kind.includes('potion') ? 'potion' : 'pickup', { pitch: kind === 'essence' ? 1.1 : 0.9 });
        if (kind === 'weapon' || kind === 'relic') this.#twin?.onCelebrate();
        break;
      }
      case 'LEVEL_UP':
        if (player) this.#vfx.levelUp({ x: player.x, y: player.y });
        audio.play('levelup');
        this.#twin?.onCelebrate();
        break;
      case 'PLAYER_HEALED':
      case 'ITEM_USED':
        if (player && Number(e.data.healed ?? e.data.amount ?? 0) > 0) this.#vfx.heal({ x: player.x, y: player.y }, Number(e.data.healed ?? e.data.amount));
        audio.play('potion');
        break;
      case 'ROOM_EXIT':
        this.#vfx.roomTransition();
        audio.play('door');
        break;
      case 'ROOM_CLEARED':
        audio.play('room_clear');
        if (player) this.#vfx.callout({ x: player.x, y: player.y }, 'ROOM CLEARED', '#f0c060', 15);
        this.#twin?.onCelebrate();
        break;
      case 'TWIN_ATTACKED':
        this.#twin?.onAttack();
        audio.play(String(e.data.weapon).includes('frost') ? 'ice' : String(e.data.weapon).includes('bow') ? 'arrow' : 'slash', { volume: 0.45 });
        break;
      case 'TWIN_DAMAGED':
        this.#twin?.onHurt();
        break;
      case 'TWIN_DOWNED':
        audio.play('twin_down');
        break;
      case 'TWIN_REVIVED':
        this.#twin?.onCelebrate();
        break;
      case 'TWIN_ACTION': {
        const intent = String(e.data.intent);
        if (intent === 'INTERCEPT' || intent === 'RETREAT' || intent === 'DISTRACT') audio.play('twin_action', { volume: 0.5 });
        if (intent === 'EXPLORE') this.#twin?.onLookAround();
        break;
      }
      case 'PLAYER_DIED':
        audio.play('death');
        this.#vfx.shake(CAMERA.shake.heavy, 400);
        break;
      case 'PLAYER_RESPAWNED':
        this.#vfx.fadeIn(600);
        break;
      case 'BOSS_COUNTER': {
        const label = {
          kite: 'keeps its distance', rush: 'closes in', dodge_aoe: 'reads the burst', riposte: 'punishes the swing',
          predict_dash: 'predicts the dash', deny_zone: 'takes your ground',
        }[String(e.data.counter)] ?? String(e.data.counter);
        this.#vfx.callout(this.#pos(e), `THE MIRROR ${label.toUpperCase()}`, '#f5a4c0', 12);
        audio.play('boss_counter', { volume: 0.6 });
        break;
      }
      case 'BOSS_NOVA_CHARGE':
        this.#vfx.telegraphRing(this.#pos(e), Number(e.data.radius), Number(e.data.duration));
        break;
      case 'BOSS_NOVA':
        this.#vfx.nova(this.#pos(e), Number(e.data.radius));
        this.#vfx.shake(CAMERA.shake.heavy, 300);
        audio.play('nova');
        break;
      case 'ENEMY_ATTACKED':
        if (e.data.ranged) audio.play('arrow', { volume: 0.5, pitch: 0.8 });
        break;
      case 'SKILL_UNLOCKED':
        audio.play('levelup', { volume: 0.5 });
        break;
      default:
        break;
    }
  }

  // --- commands & settings ---------------------------------------------------------------

  #onCommand(cmd: CommandMessage): void {
    this.#ws.sendCommand(cmd);
    audio.play('ui_click', { volume: 0.5 });
  }

  #applySettings(settings: Settings): void {
    const qualityChanged = settings.quality !== this.#settings.quality;
    this.#settings = settings;
    this.cameras.main.setZoom(RENDER_SCALE * settings.zoom);
    this.#vfx.setSettings(settings);
    audio.applySettings(settings);
    if (this.#twin) this.#twin.showThoughts = settings.showTwinThoughts;
    if (qualityChanged) {
      this.#world.setQuality(settings.quality);
      this.#ambient.setQuality(settings.quality);
      if (this.#room) this.#world.build(this.#room);
    }
    if (!settings.debugOverlay) this.#debug.clear();
  }

  // --- debug --------------------------------------------------------------------------------

  #drawDebug(): void {
    const g = this.#debug;
    if (!this.#settings.debugOverlay || !this.#snapshot) return;
    g.clear();
    const snap = this.#snapshot;
    const cell = snap.playerModel.cellSize || 64;
    const layers: Array<[string, number]> = [['combat', 0xd62e6c], ['retreat', 0x4f8fe6], ['dodge', 0xa0cae4], ['death', 0x000000]];
    for (const [layer, colour] of layers) {
      const cells = snap.playerModel.spatial[layer] ?? [];
      const max = cells[0]?.weight ?? 1;
      for (const c of cells) {
        g.fillStyle(colour, 0.08 + 0.32 * (c.weight / max));
        g.fillRect(c.cell[0] * cell, c.cell[1] * cell, cell, cell);
      }
    }
    // Twin intent.
    const twin = snap.twin;
    const goal = twin.intent.position ?? (twin.intent.targetId ? snap.enemies.find((e) => e.id === twin.intent.targetId)?.position : null);
    if (goal) {
      g.lineStyle(2, 0xa0cae4, 0.8);
      g.lineBetween(twin.position.x, twin.position.y, goal.x, goal.y);
      g.strokeCircle(goal.x, goal.y, 10);
    }
    // Enemy targeting.
    for (const e of snap.enemies) {
      const target = e.targetId === snap.player.id ? snap.player.position : e.targetId === twin.id ? twin.position : null;
      if (target) {
        g.lineStyle(1, e.windingUp ? 0xff4d4d : 0xffffff, e.windingUp ? 0.9 : 0.25);
        g.lineBetween(e.position.x, e.position.y, target.x, target.y);
      }
      // Two circles, because they are two different things: the faint one is
      // how much room the body takes up (separation, walls), the solid one is
      // what a swing or an arrow is actually tested against.
      g.lineStyle(1, 0xffffff, 0.15);
      g.strokeCircle(e.position.x, e.position.y, e.radius);
      g.lineStyle(1, PALETTE.gold, 0.45);
      g.strokeCircle(e.position.x, e.position.y, e.hitRadius ?? e.radius);
    }
    // Player facing.
    const p = snap.player;
    g.lineStyle(2, 0x63c26d, 0.9);
    g.lineBetween(p.position.x, p.position.y, p.position.x + p.facing.x * 40, p.position.y + p.facing.y * 40);
  }

  #dispose(): void {
    for (const off of this.#teardown) off();
    this.#teardown = [];
    this.#source.destroy();
    this.#ws.disconnect();
    this.#world.destroy();
    this.#ambient.destroy();
    // An enemy load started in the last room can land after this; without
    // this it would try to upgrade views belonging to a dead scene.
    this.#enemyAtlases.stop();
    this.#enemies.clear();
  }
}

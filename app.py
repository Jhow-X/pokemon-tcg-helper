from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
import json, os, random, uuid

app = Flask(__name__)
app.config['SECRET_KEY'] = 'pokemon_tcg_secret_key'
socketio = SocketIO(app, cors_allowed_origins="*")

# -----------------------------
#  CLASSES PRINCIPAIS
# -----------------------------
class Pokemon:
    def __init__(self, name="", max_hp=0, current_hp=0):
        self.name = name
        self.max_hp = max_hp
        self.current_hp = current_hp
        self.status_effects = []
        self.damage_counters = 0
        
    def to_dict(self):
        return vars(self)

    def from_dict(self, data):
        for k, v in data.items():
            setattr(self, k, v)


class GameState:
    def __init__(self, room_id=None):
        self.player1 = self._new_player()
        self.player2 = self._new_player()
        self.knockout_log = []
        self.game_ended = False
        self.winner = None
        self.room_id = (room_id or str(uuid.uuid4())[:8]).upper()

    # -----------------------------
    #  HELPERS
    # -----------------------------
    def _new_player(self):
        return {
            'active': Pokemon(),
            'bench': [Pokemon() for _ in range(5)],
            'prize_cards': 6,
            'connected': False,
            'socket_id': None,
            'name': ''
        }

    def get_player(self, key):
        return getattr(self, key)

    def get_opponent(self, key):
        return self.player2 if key == "player1" else self.player1

    def get_target(self, player_key, position):
        player = self.get_player(player_key)
        return player['active'] if position == "active" else player['bench'][int(position)]

    # -----------------------------
    #  ESTADOS / KO / VITÓRIA
    # -----------------------------
    def check_and_remove_knocked_out(self, attacking_player=None):
        knockouts = []
        for p_key in ["player1", "player2"]:
            player = getattr(self, p_key)
            opponent_key = "player1" if p_key == "player2" else "player2"

            # Ativo KO
            if player["active"].name and player["active"].current_hp <= 0:
                knockouts.append(self._make_ko_info(player["active"], p_key, "active", attacking_player, opponent_key))
                player["active"] = Pokemon()

            # Banco KO
            for i, mon in enumerate(player["bench"]):
                if mon.name and mon.current_hp <= 0:
                    knockouts.append(self._make_ko_info(mon, p_key, f"bench-{i}", attacking_player, opponent_key))
                    player["bench"][i] = Pokemon()

            # Aplicar prêmio
            if attacking_player and attacking_player != p_key:
                att_player = getattr(self, attacking_player)
                att_player['prize_cards'] = max(0, att_player['prize_cards'] - len([k for k in knockouts if k["owner"] == p_key]))

        self.knockout_log.extend(knockouts)
        self.check_victory_condition()
        return knockouts

    def _make_ko_info(self, mon, owner, position, attacking, opponent):
        return {
            'pokemon_name': mon.name,
            'owner': owner,
            'position': position,
            'defeated_by': attacking if attacking != owner else opponent
        }

    def check_victory_condition(self):
        if self.player1['prize_cards'] <= 0:
            self.game_ended = True
            self.winner = 'player1'
        elif self.player2['prize_cards'] <= 0:
            self.game_ended = True
            self.winner = 'player2'

    def to_dict(self):
        return {
            'player1': self._player_to_dict(self.player1),
            'player2': self._player_to_dict(self.player2),
            'knockout_log': self.knockout_log,
            'game_ended': self.game_ended,
            'winner': self.winner,
            'room_id': self.room_id
        }

    def _player_to_dict(self, player):
        return {
            'active': player['active'].to_dict(),
            'bench': [p.to_dict() for p in player['bench']],
            'prize_cards': player['prize_cards'],
            'connected': player['connected'],
            'name': player['name']
        }


# -----------------------------
#  GERENCIAMENTO DE SALAS
# -----------------------------
game_rooms = {}

def normalize_room(room_id):
    return room_id.upper() if room_id else None

def get_random_victory_sound():
    sounds_dir = os.path.join(app.static_folder, 'sounds')
    if not os.path.exists(sounds_dir): return None
    mp3 = [f for f in os.listdir(sounds_dir) if f.lower().endswith(".mp3")]
    return random.choice(mp3) if mp3 else None


# -----------------------------
#  ROTAS
# -----------------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/room/<room_id>')
def join_room_page(room_id):
    return render_template('index.html', room_id=room_id)


# -----------------------------
#  SOCKET EVENTS
# -----------------------------
@socketio.on('connect')
def on_connect():
    print(f'Cliente conectado: {request.sid}')

@socketio.on('disconnect')
def on_disconnect():
    for room_id, gs in game_rooms.items():
        for p in ["player1", "player2"]:
            if gs.get_player(p)['socket_id'] == request.sid:
                gs.get_player(p)['connected'] = False
                gs.get_player(p)['socket_id'] = None
                socketio.emit('game_state_update', gs.to_dict(), room=room_id)


@socketio.on('join_game')
def on_join_game(data):
    room_id = normalize_room(data.get("room_id"))
    player_name = data.get("player_name", "Jogador")

    # Criar sala se não existir
    if not room_id or room_id not in game_rooms:
        gs = GameState(room_id=room_id)
        game_rooms[gs.room_id] = gs
        room_id = gs.room_id
    else:
        gs = game_rooms[room_id]

    join_room(room_id)

    # Atribuir jogador
    assigned = None
    for p in ["player1", "player2"]:
        if not gs.get_player(p)['connected']:
            gs.get_player(p).update({
                'connected': True,
                'socket_id': request.sid,
                'name': player_name
            })
            assigned = p
            break

    emit('player_assigned', {
        'player': assigned,
        'room_id': room_id,
        'game_state': gs.to_dict()
    })

    socketio.emit('game_state_update', gs.to_dict(), room=room_id)


# -----------------------------
#  HANDLER UTILITÁRIO
# -----------------------------
def update_and_emit(game_state, room_id, knockouts=None):
    socketio.emit('game_state_update', game_state.to_dict(), room=room_id)
    if knockouts:
        socketio.emit('knockouts_occurred', {'knockouts': knockouts}, room=room_id)
    if game_state.game_ended:
        socketio.emit('game_ended', {'winner': game_state.winner}, room=room_id)


# -----------------------------
#  UPDATE POKÉMON
# -----------------------------
@socketio.on('update_pokemon')
def on_update_pokemon(data):
    room_id = normalize_room(data['room_id'])
    gs = game_rooms.get(room_id)
    if not gs: return

    target = gs.get_target(data['player'], data['position'])
    target.from_dict(data['pokemon'])

    ko = gs.check_and_remove_knocked_out()
    update_and_emit(gs, room_id, ko)


# -----------------------------
#  APLICA DANO
# -----------------------------
@socketio.on('apply_damage')
def on_apply_damage(data):
    room_id = normalize_room(data['room_id'])
    gs = game_rooms.get(room_id)
    if not gs: return

    target = gs.get_target(data['player'], data['position'])
    target.current_hp = max(0, target.current_hp - int(data['damage']))

    ko = gs.check_and_remove_knocked_out(data.get('attacking_player'))
    update_and_emit(gs, room_id, ko)


# -----------------------------
#  CURAR
# -----------------------------
@socketio.on('heal_pokemon')
def on_heal(data):
    room_id = normalize_room(data['room_id'])
    gs = game_rooms.get(room_id)
    if not gs: return

    target = gs.get_target(data['player'], data['position'])
    target.current_hp = min(target.max_hp, target.current_hp + int(data['heal']))

    update_and_emit(gs, room_id)


# -----------------------------
#  CONTADORES DE DANO
# -----------------------------
@socketio.on('update_damage_counters')
def on_counters(data):
    room_id = normalize_room(data['room_id'])
    gs = game_rooms.get(room_id)
    if not gs: return

    target = gs.get_target(data['player'], data['position'])
    target.damage_counters = max(0, int(data['counters']))

    update_and_emit(gs, room_id)


# -----------------------------
#  STATUS
# -----------------------------
@socketio.on('add_status')
def on_add_status(data):
    room_id = normalize_room(data['room_id'])
    gs = game_rooms.get(room_id)
    if not gs: return

    target = gs.get_target(data['player'], data['position'])
    if data['status'] not in target.status_effects:
        target.status_effects.append(data['status'])

    update_and_emit(gs, room_id)

@socketio.on('remove_status')
def on_remove_status(data):
    room_id = normalize_room(data['room_id'])
    gs = game_rooms.get(room_id)
    if not gs: return

    target = gs.get_target(data['player'], data['position'])
    if data['status'] in target.status_effects:
        target.status_effects.remove(data['status'])

    update_and_emit(gs, room_id)


# -----------------------------
#  PRIZE CARDS
# -----------------------------
@socketio.on('update_prize_cards')
def on_prize_cards(data):
    room_id = normalize_room(data['room_id'])
    gs = game_rooms.get(room_id)
    if not gs: return

    gs.get_player(data['player'])['prize_cards'] = int(data['prize_cards'])
    gs.check_victory_condition()

    update_and_emit(gs, room_id)


# -----------------------------
#  SWAP
# -----------------------------
@socketio.on('swap_pokemon')
def on_swap(data):
    room_id = normalize_room(data['room_id'])
    gs = game_rooms.get(room_id)
    if not gs: return

    player = gs.get_player(data['player'])
    idx = int(data['bench_index'])

    player['active'], player['bench'][idx] = player['bench'][idx], player['active']

    ko = gs.check_and_remove_knocked_out()
    update_and_emit(gs, room_id, ko)


# -----------------------------
#  RESET GAME
# -----------------------------
@socketio.on('reset_game')
def on_reset(data):
    room_id = normalize_room(data['room_id'])
    if room_id not in game_rooms: return

    old = game_rooms[room_id]
    new = GameState(room_id)

    # Mantém jogadores e conexões
    for p in ["player1", "player2"]:
        new_p = new.get_player(p)
        old_p = old.get_player(p)
        new_p['connected'] = old_p['connected']
        new_p['socket_id'] = old_p['socket_id']
        new_p['name'] = old_p['name']

    game_rooms[room_id] = new
    update_and_emit(new, room_id)


# -----------------------------
#  SOM DE VITÓRIA
# -----------------------------
@socketio.on('get_victory_sound')
def on_get_sound():
    s = get_random_victory_sound()
    emit('victory_sound', {
        'sound_file': s,
        'sound_url': f'/static/sounds/{s}' if s else None
    })


# -----------------------------
#  RUN SERVER
# -----------------------------
if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)

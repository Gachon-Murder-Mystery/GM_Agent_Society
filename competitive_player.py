"""Scripted competitive player and random player (C-8). No LLM; for engine/GM checks and baselines.

Formats are assumptions until the engine's hypothesis-question API (A-9) lands:
- clue: {"id", "kind": "definite", "category": "suspect"|"weapon"|"place", "candidate"}
        {"id", "kind": "indirect", "type": "character_room", "character", "room"}
        {"id", "kind": "indirect", "type": "companion", "character", "other"}
        {"id", "kind": "indirect", "type": "weapon_room", "weapon", "room"}
  A narrated indirect clue from a player ({"id", "kind": "narrated", "text"}) carries no
  parsable fact for a scripted player and is kept only as an ID.
- question: {"action": "question", "target_id", "approach": "question"|"negotiate"|"interrogate"|"threaten",
             "hypothesis": {"suspect", "weapon", "place"}}
- response: {"form": "none"|"ambiguous"|"definite"|"definite+ambiguous", "statements": [...], "reason"}
Negotiation (A-10) is not scripted yet: its message format is still open.
"""
import random

SIZES = {"suspect": 12, "weapon": 8, "place": 8}
CATEGORIES = tuple(SIZES)
# Missions a scripted player pursues by standing in a room (design doc 2.5-2.6). M09 uses the start room.
MISSION_ROOMS = {"O1": 5, "M45": 5, "O3": 7, "M50": 7, "M02": 2, "M03": 3, "M04": 4, "M05": 6, "M06": 1}
PURSUE_WHILE_COMBOS = 8  # pursue a room mission while more than this many answers remain (default)


class Knowledge:
    """Exclusions plus midnight facts; `infer` combines them (design doc 1.3)."""

    def __init__(self):
        self.excluded = {cat: {} for cat in CATEGORIES}  # candidate -> supporting clue IDs
        self.char_room, self.weapon_room = {}, {}           # id -> (room, support)
        self.parent = list(range(SIZES["suspect"]))
        self.group_support = {}
        self.clue_ids = set()

    def _find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def _exclude(self, category, candidate, support):
        if candidate in self.excluded[category]:
            return 0
        self.excluded[category][candidate] = frozenset(support)
        return 1

    def add(self, clue):
        self.clue_ids.add(clue["id"])
        if clue["kind"] == "definite":
            self._exclude(clue["category"], clue["candidate"], {clue["id"]})
        elif clue["kind"] == "indirect":
            if clue["type"] == "character_room":
                self.char_room.setdefault(clue["character"], (clue["room"], frozenset({clue["id"]})))
            elif clue["type"] == "weapon_room":
                self.weapon_room.setdefault(clue["weapon"], (clue["room"], frozenset({clue["id"]})))
            elif clue["type"] == "companion":
                a, b = self._find(clue["character"]), self._find(clue["other"])
                support = self.group_support.pop(a, frozenset()) | self.group_support.pop(b, frozenset()) | {clue["id"]}
                self.parent[a] = b
                self.group_support[b] = frozenset(support)

    def remaining(self, category):
        return [c for c in range(SIZES[category]) if c not in self.excluded[category]]

    def combinations(self):
        result = 1
        for category in CATEGORIES:
            result *= len(self.remaining(category))
        return result

    def infer(self):
        """Apply the combination rules to a fixpoint; returns the number of new exclusions derived."""
        steps, changed = 0, True
        while changed:
            before = steps
            for x in range(SIZES["suspect"]):  # companions share a room
                if x in self.char_room:
                    continue
                root = self._find(x)
                for y, (room, support) in list(self.char_room.items()):
                    if self._find(y) == root:
                        self.char_room[x] = (room, support | self.group_support.get(root, frozenset()))
                        break
            places = self.excluded["place"]
            for category, located in (("suspect", self.char_room), ("weapon", self.weapon_room)):
                for item, (room, support) in located.items():
                    if room in places:  # at an excluded room at midnight -> not the culprit/weapon
                        steps += self._exclude(category, item, support | places[room])
            scene = self.remaining("place")
            if len(scene) == 1:  # scene known -> anyone/anything elsewhere at midnight is excluded
                all_places = frozenset().union(*places.values())
                for category, located in (("suspect", self.char_room), ("weapon", self.weapon_room)):
                    for item, (room, support) in located.items():
                        if room != scene[0]:
                            steps += self._exclude(category, item, support | all_places)
            for category, located in (("suspect", self.char_room), ("weapon", self.weapon_room)):
                left = self.remaining(category)
                if len(left) == 1 and left[0] in located:  # known culprit/weapon location -> scene
                    room, support = located[left[0]]
                    basis = support | frozenset().union(*self.excluded[category].values())
                    for place in range(SIZES["place"]):
                        if place != room:
                            steps += self._exclude("place", place, basis)
            changed = steps > before
        return steps

    def evidence(self):
        return sorted(set().union(*(s for cat in CATEGORIES for s in self.excluded[cat].values())))


class CompetitivePlayer:
    """Hides by default, builds Cluedo-style hypotheses, challenges only when (nearly) certain."""

    def __init__(self, agent_id, sheet, mbti, seed, missions=(), names=None, places=None):
        self.agent_id, self.sheet, self.mbti = agent_id, sheet, mbti
        self.rng = random.Random(seed)
        self.knowledge = Knowledge()
        self.held = []                      # raw clues this player can answer with
        self.room = sheet["start_room"]
        self.attempts, self.exhausted = {}, set()
        self.asked_me = {}                  # asker -> hypotheses they used on me (their prior, estimated)
        self.relationship = {}              # my value toward others, as the engine shows it
        self.missions = [m["mission_id"] if isinstance(m, dict) else m for m in missions]
        self.achieved, self.cycle = set(), 0
        # S: challenge only with one answer left; N: also with two (faster, risk of death). B7 tendency.
        self.risk = 2 if mbti[1] == "N" else 1
        self.names, self.places = names or {}, places or {}

    # ---------- observation ----------

    def observe(self, observation):
        self.room = observation.get("room", self.room)
        self.cycle = observation.get("cycle", self.cycle)
        self.relationship.update(observation.get("relationship", {}))
        self.achieved.update(observation.get("achieved_missions", []))

    def ingest(self, result):
        clues = result.get("clues", [])
        for clue in clues:
            if clue["id"] not in self.knowledge.clue_ids and clue["kind"] in ("definite", "indirect"):
                self.held.append(clue)
            self.knowledge.add(clue)
        if result.get("action") == "explore" and result.get("target", "room") == "room":
            self.attempts[self.room] = self.attempts.get(self.room, 0) + 1
            if result.get("successes", 0) >= 1 and not clues:
                self.exhausted.add(self.room)  # a successful search found nothing new here

    # ---------- own turn ----------

    def _mission_room(self):
        if self.knowledge.combinations() <= PURSUE_WHILE_COMBOS:
            return None
        for mission in self.missions:
            if mission in self.achieved:
                continue
            if mission == "M09" and self.cycle <= 10:
                return self.sheet["start_room"]
            if mission in MISSION_ROOMS:
                return MISSION_ROOMS[mission]
        return None

    def move(self, observation=None):
        if observation:
            self.observe(observation)
        goal = self._mission_room()
        if goal is None:
            if self.room not in self.exhausted:
                return {"action": "stay"}
            open_rooms = [r for r in range(SIZES["place"]) if r not in self.exhausted]
            if not open_rooms:
                return {"action": "stay"}
            goal = min(open_rooms, key=lambda r: (self.attempts.get(r, 0), self.rng.random()))
        return {"action": "stay"} if goal == self.room else {"action": "move", "room": goal}

    def hypothesis(self):
        """Probe one unknown candidate in the widest category; fill the rest with excluded ones (B3)."""
        widest = max(CATEGORIES, key=lambda c: (len(self.knowledge.remaining(c)), self.rng.random()))
        result = {}
        for category in CATEGORIES:
            left, gone = self.knowledge.remaining(category), sorted(self.knowledge.excluded[category])
            pool = left if category == widest or not gone else gone
            result[category] = self.rng.choice(pool)
        return result

    def interact(self, observation):
        self.observe(observation)
        here = observation.get("here", [])
        corpse = next((c for c in here if c["kind"] == "corpse"), None)
        if corpse:
            return {"action": "explore", "target": f"corpse:{corpse['id']}"}
        if self.room not in self.exhausted:
            return {"action": "explore", "target": "room"}
        people = [c for c in here if c["kind"] in ("player", "npc") and c["id"] != self.agent_id]
        if not people:
            return {"action": "prepare"}
        npcs = [c for c in people if c["kind"] == "npc"]  # NPCs answer with the raw text
        target = self.rng.choice(npcs or people)
        stats = self.sheet["stats"]
        if stats["pressure"] > stats["speech"]:
            approach = "threaten" if stats["pressure"] == 5 and target["kind"] == "player" else "interrogate"
        else:
            approach = "question"
        return {"action": "question", "target_id": target["id"], "approach": approach, "hypothesis": self.hypothesis()}

    def deduce(self):
        steps = self.knowledge.infer()
        combos = self.knowledge.combinations()
        remaining = {c: self.knowledge.remaining(c) for c in CATEGORIES}
        decision = {"remaining": {c: len(v) for c, v in remaining.items()}, "confidence": 1 / combos,
                    "reasoning_tokens": 0, "reasoning_steps": steps,  # scripted: no LLM tokens
                    "evidence_ids": self.knowledge.evidence()}
        if combos <= self.risk:
            decision.update(decision="challenge", accusation={c: self.rng.choice(v) for c, v in remaining.items()})
        else:
            decision.update(decision="hold", accusation=None)
        return decision

    # ---------- answering (does not consume an action) ----------

    def _about(self):
        return {(c["category"], c["candidate"]) for c in self.held if c["kind"] == "definite"}

    def _asker_prior(self, asker):
        """Candidates the asker has already put in hypotheses to me: a rough guess at what they know."""
        return {(cat, cand) for hyp in self.asked_me.get(asker, []) for cat, cand in hyp.items()}

    def _connects(self, clue, prior):
        if clue["type"] == "character_room" or clue["type"] == "weapon_room":
            return ("place", clue["room"]) in prior
        return False  # a companion clue needs a known location; assume it does not connect

    def narrate(self, clue):
        """Vague retelling: always omits a part, never adds or concludes (design doc 1.4)."""
        place = self.places.get(clue.get("room"), "어느 칸")
        if clue["type"] == "character_room":
            return f"자정쯤 {place}에 누가 있었던 건 확실해요."
        if clue["type"] == "companion":
            return f"{self.names.get(clue['character'], '그 사람')} 씨는 그날 밤 혼자가 아니었어요."
        return f"그 시간 {place} 어딘가에 흉기가 하나 있었다고 들었어요."

    def _soft(self, hypothesis, prior, limit):
        """Up to `limit` non-definite statements: true ambiguous ones first, then least-connecting indirect clues."""
        about = self._about()
        statements = [{"kind": "ambiguous", "category": cat, "candidate": cand}
                      for cat, cand in hypothesis.items() if (cat, cand) not in about]
        indirect = sorted((c for c in self.held if c["kind"] == "indirect"),
                          key=lambda c: (self._connects(c, prior), c["type"] != "companion", c["id"]))
        statements += [{"kind": "indirect", "clue_id": c["id"], "narration": self.narrate(c)} for c in indirect]
        return statements[:limit]

    def _definite(self, prior):
        own = [c for c in self.held if c["kind"] == "definite"]
        if not own:
            return None
        known = [c for c in own if (c["category"], c["candidate"]) in prior]  # give what they likely have
        return {"kind": "definite", "clue_id": self.rng.choice(known or own)["id"]}

    def respond(self, request):
        asker, hypothesis, successes = request["asker_id"], request["hypothesis"], request["successes"]
        prior = self._asker_prior(asker)
        self.asked_me.setdefault(asker, []).append(dict(hypothesis))
        if not self.held or successes <= 0:
            return {"form": "none", "statements": [], "reason": "보유 단서 없음" if not self.held else "0성공"}
        soft, definite = self._soft(hypothesis, prior, 2), self._definite(prior)
        warm = self.mbti[2] == "F" and self.relationship.get(asker, 50) >= 70
        if successes == 1:
            if soft:
                return {"form": "ambiguous", "statements": soft[:1], "reason": "1성공: 가장 덜 드러나는 진술"}
            return {"form": "definite", "statements": [definite], "reason": "참인 모호 진술이 없어 확실 단서로 대체"}
        if successes == 2:
            if (warm or len(soft) < 2) and definite:
                why = "F 성향·관계성 70 이상" if warm else "모호 진술 2개를 만들 수 없음"
                return {"form": "definite", "statements": [definite], "reason": f"2성공: 확실 1 ({why})"}
            return {"form": "ambiguous", "statements": soft, "reason": "2성공: 숨김 전략으로 모호 2"}
        statements = ([definite] if definite else []) + soft
        return {"form": "definite+ambiguous", "statements": statements,
                "reason": "3성공: 상대가 이미 가졌을 법한 확실 단서 + 모호 2"}


class RandomPlayer(CompetitivePlayer):
    """Baseline (spec D2): legal but unreasoned choices."""

    def __init__(self, *args, challenge_rate=0.02, **kwargs):
        super().__init__(*args, **kwargs)
        self.challenge_rate = challenge_rate

    def move(self, observation=None):
        if observation:
            self.observe(observation)
        room = self.rng.randrange(SIZES["place"])
        return {"action": "stay"} if room == self.room else {"action": "move", "room": room}

    def interact(self, observation):
        self.observe(observation)
        people = [c for c in observation.get("here", []) if c["kind"] in ("player", "npc") and c["id"] != self.agent_id]
        options = ["explore", "prepare"] + (["question"] if people else [])
        kind = self.rng.choice(options)
        if kind == "question":
            return {"action": "question", "target_id": self.rng.choice(people)["id"],
                    "approach": self.rng.choice(["question", "negotiate", "interrogate", "threaten"]),
                    "hypothesis": {c: self.rng.randrange(n) for c, n in SIZES.items()}}
        return {"action": "explore", "target": "room"} if kind == "explore" else {"action": "prepare"}

    def deduce(self):
        decision = {"remaining": None, "confidence": None, "reasoning_tokens": 0, "reasoning_steps": 0,
                    "evidence_ids": []}
        if self.rng.random() < self.challenge_rate:
            decision.update(decision="challenge", accusation={c: self.rng.randrange(n) for c, n in SIZES.items()})
        else:
            decision.update(decision="hold", accusation=None)
        return decision

    def respond(self, request):
        answer = super().respond(request)
        if request["successes"] == 2 and answer["form"] != "none":
            soft, definite = self._soft(request["hypothesis"], set(), 2), self._definite(set())
            if definite and (len(soft) < 2 or self.rng.random() < 0.5):
                return {"form": "definite", "statements": [definite], "reason": "무작위 선택"}
            if len(soft) == 2:
                return {"form": "ambiguous", "statements": soft, "reason": "무작위 선택"}
        return answer

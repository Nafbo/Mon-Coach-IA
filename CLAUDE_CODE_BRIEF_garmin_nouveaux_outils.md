# Brief pour Claude Code — 3 nouveaux tools Garmin MCP

## Contexte

Tu travailles sur `mcp-garmin-coach`, un serveur MCP Python (SDK MCP officiel, Streamable HTTP)
qui expose des données Garmin Connect et un état opérationnel (plan d'entraînement) à un Project
claude.ai de coaching triathlon. La spec initiale complète est dans `mcp-garmin-coach-spec.md` à
la racine du repo — lis-la pour comprendre les conventions déjà en place (structure des tools,
gestion d'erreur, logging, fuseau horaire, cache) avant de commencer.

Ce brief documente **3 tools supplémentaires**, déjà spécifiés et validés avec l'utilisateur.
Ton travail : les implémenter, avec tests, **en local uniquement**.

## Règles strictes — à respecter absolument

- **Ne touche à rien dans `deploy/`**, ne modifie pas `.env`, ne fais aucune action réseau vers
  la VM de prod, ne push rien sur un remote. Ce travail reste 100% local jusqu'à validation
  manuelle par l'utilisateur.
- **Travaille sur une branche dédiée** (ex. `feature/garmin-weather-details-push`), pas sur
  `main`/`master` directement.
- **`garmin_push_workout` doit avoir `dry_run=True` par défaut** — aucun appel réel d'écriture
  vers Garmin (`upload_workout`/`schedule_workout`) ne doit pouvoir se déclencher sans que
  l'utilisateur l'ait explicitement demandé (`dry_run=False`).
- **Périmètre v1 : course + vélo uniquement** pour `garmin_push_workout`. La natation est hors
  scope de cette itération (cf. section dédiée plus bas) — ne l'implémente pas, mais ne casse
  rien qui l'empêcherait d'être ajoutée proprement plus tard.
- Termine par `pytest` (depuis la racine du repo) et assure-toi que **tous les tests passent**,
  anciens et nouveaux, avant de considérer le travail fini.
- Documente tes écarts par rapport à ce brief dans le README, section "Écarts volontaires par
  rapport à la spec initiale" (section déjà existante, suis son style) — exactement comme les
  précédents tools l'ont fait quand la réalité de l'API Garmin différait de l'hypothèse initiale.

## Étape 0 — Valider le format réel weather/details AVANT de coder le parsing

Un script `inspect_new_endpoints.py` existe déjà à la racine du repo (lecture seule, aucune
écriture). **Lance-le en premier** (`python inspect_new_endpoints.py`, venv activé, `.env` déjà
configuré) pour récupérer le JSON réel renvoyé par `get_activity_weather` et
`get_activity_details` sur une vraie activité de l'historique. Utilise cette sortie réelle pour
écrire le parsing — ne devine pas la structure à partir de la doc Garmin non officielle. Une fois
utilisé, tu peux supprimer `garmin_details_sample.json` généré (données personnelles) mais garde
le script (utile pour la prochaine évolution du serveur).

Si pour une raison quelconque le script échoue (MFA à refaire, etc.), arrête-toi et demande à
l'utilisateur de le lancer lui-même puis de te coller la sortie — ne code pas le parsing sur une
supposition.

## Vérifier/durcir la contrainte de version `garminconnect`

`pyproject.toml` déclare actuellement `garminconnect>=0.2.19`, mais la version réellement
installée dans le `.venv` du projet est `0.3.2`, et c'est cette version qui expose
`get_activity_weather`, `get_activity_details`, et le module `garminconnect.workout`
(`RunningWorkout`/`CyclingWorkout`/`upload_workout`/`schedule_workout`/etc.). Resserre la
contrainte à `garminconnect>=0.3.2` (ou la version minimale que tu confirmes contenir ces
méthodes) pour ne pas casser silencieusement le serveur chez quelqu'un avec une version plus
ancienne installée.

Ajoute aussi `pydantic>=2.0.0` explicitement en dépendance (utilisé par `garminconnect.workout`
pour les workouts typés) — il est présent dans le `.venv` actuel mais pas déclaré dans
`pyproject.toml`, ce qui marche par accident plutôt que par contrat.

---

## Tool 1 — `garmin_get_activity_weather(activity_id)`

- Input : `{ "activity_id": "<str|int>" }` (obligatoire, pas de défaut)
- Ajoute une méthode `GarminClient.get_activity_weather(activity_id)` dans `garmin_client.py`,
  wrapper direct de `self._call(self._garmin.get_activity_weather, str(activity_id))`, avec le
  même parsing défensif que le reste du fichier (`_to_int`, `_round_or_none`, valeurs `None` si
  champ absent plutôt qu'un crash) — champs exacts déterminés à l'étape 0.
- Ajoute le tool `garmin_get_activity_weather` dans `tools/garmin_tools.py`, même structure que
  les autres `garmin_get_*` (capture d'exception → `_garmin_error_message`).
- **Cache sans expiration**, pas la fenêtre 1h (`CACHE_FRESHNESS`) utilisée par les autres tools
  `garmin_get_*` : la météo d'une activité déjà terminée est immuable. Réutilise la table
  `garmin_cache` existante (`type="activity_weather"`, `date` = `activity_id` en guise de clé) ;
  ajoute une variante de `_get_cached` qui ignore `CACHE_FRESHNESS` pour ce type (ou un paramètre
  `ignore_freshness: bool` sur la fonction existante — à toi de choisir la forme la plus propre).

## Tool 2 — `garmin_get_activity_details(activity_id)`

- Input : `{ "activity_id": "<str|int>" }`
- Méthode `GarminClient.get_activity_details(activity_id)` : appelle
  `self._garmin.get_activity_details(str(activity_id))`, puis **retraite** le format brut (dont
  la forme exacte sort de l'étape 0) pour produire 3 séries alignées sur un axe temps commun :
  fréquence cardiaque, allure ou vitesse, dénivelé. Le format brut Garmin pour cet endpoint
  utilise typiquement un couple `metricDescriptors` (quelle colonne = quelle grandeur) +
  `activityDetailMetrics` (points bruts) — confirme et adapte selon la sortie réelle de l'étape 0.
  Ce n'est pas un passthrough JSON : le calcul de découplage cardiaque (déjà présent dans les
  skills course-à-pied et vélo du Project claude.ai) consomme ces 3 séries directement.
- Tool `garmin_get_activity_details` dans `tools/garmin_tools.py`, même pattern que le tool 1.
- Cache sans expiration, même principe que weather (clé `activity_id`).
- Les payloads peuvent être volumineux (séries sur 1-2h d'activité) — pas un problème en usage
  perso SQLite, mais ne les fais pas transiter deux fois inutilement dans le code.

## Tool 3 — `garmin_push_workout(date, name, structure)`

**Objectif** : créer et programmer une séance structurée sur la montre, pour les séances
**Seuil/VMA/VO2max uniquement** (jamais endurance/sortie longue/technique/récupération/renfo —
cette règle est appliquée en amont par les skills de discipline, le tool lui-même n'a pas besoin
de la revalider, mais ne fais rien qui la contredise).

### Nouveau module `src/workout_builder.py`

Un seul fichier (pas un fichier par discipline — cohérent avec le pattern déjà en place dans
`garmin_client.py`). Fonctions pures, testables hors-ligne (aucun appel réseau, aucune
dépendance à `GarminClient`) :

- `build_workout(structure: dict) -> dict` — dispatcher : lit `structure["discipline"]`
  (`"course"` ou `"velo"`), construit un `RunningWorkout` ou `CyclingWorkout`
  (`garminconnect.workout`) à partir de `structure["warmup"]`, `structure["blocks"]`,
  `structure["cooldown"]`, et retourne `.to_dict()` (le JSON prêt pour `upload_workout`).
- `_build_target(target: dict | None) -> dict` — convertit un target du schéma (`pace_min_per_km`,
  `power_watts`, `hr_bpm`, ou absent) vers le `targetType`/bornes Garmin
  (`TargetType.SPEED`/`POWER`/`HEART_RATE`, `targetValueOne`/`targetValueTwo`). **Piège à ne pas
  rater** : l'allure et la vitesse sont inversement proportionnelles — un `target.low` (allure la
  plus rapide, en min/km) correspond à la vitesse la plus **haute**, donc à `targetValueTwo` (la
  borne haute côté Garmin), pas `targetValueOne`. Écris un test dédié qui vérifie ce sens de
  conversion explicitement, pas juste que les deux valeurs sont présentes.
- Utilise `create_warmup_step`/`create_interval_step`/`create_recovery_step`/
  `create_cooldown_step`/`create_repeat_group` de `garminconnect.workout` pour les steps — tous
  bornés en temps (`duration_sec`), cohérent avec le scope v1 (pas de distance).

#### Schéma de `structure` (input du tool)

```json
{
  "discipline": "course",
  "warmup": { "duration_sec": 900 },
  "blocks": [
    {
      "repeat": 6,
      "steps": [
        { "type": "interval", "duration_sec": 90, "target": { "type": "pace_min_per_km", "low": 3.33, "high": 3.5 } },
        { "type": "recovery", "duration_sec": 90 }
      ]
    }
  ],
  "cooldown": { "duration_sec": 600 }
}
```

- `discipline` : `"course"` | `"velo"`
- `target.type` : `"pace_min_per_km"` (course), `"power_watts"` (vélo), `"hr_bpm"` (les deux), ou
  `target` absent/`null` (pas de zone imposée — cas normal pour warmup/cooldown/recovery)
- Un bloc sans `repeat` (ou `repeat: 1`) est une séquence de steps à plat, même mécanique que les
  blocs répétés

Deux exemples concrets à utiliser comme fixtures de test (dans `tests/fixtures/` ou inline) :

**Course — `24/08 - Fractionné VMA 6x400`** :
```json
{
  "date": "2026-08-24",
  "name": "24/08 - Fractionné VMA 6x400",
  "dry_run": true,
  "structure": {
    "discipline": "course",
    "warmup": { "duration_sec": 900 },
    "blocks": [
      { "repeat": 6, "steps": [
        { "type": "interval", "duration_sec": 90, "target": { "type": "pace_min_per_km", "low": 3.33, "high": 3.5 } },
        { "type": "recovery", "duration_sec": 90 }
      ] }
    ],
    "cooldown": { "duration_sec": 600 }
  }
}
```

**Vélo — `27/08 - Fractionné VO2max 6x4min`** (FTP=250W, Z5 106-120%) :
```json
{
  "date": "2026-08-27",
  "name": "27/08 - Fractionné VO2max 6x4min",
  "dry_run": true,
  "structure": {
    "discipline": "velo",
    "warmup": { "duration_sec": 900 },
    "blocks": [
      { "repeat": 6, "steps": [
        { "type": "interval", "duration_sec": 240, "target": { "type": "power_watts", "low": 265, "high": 300 } },
        { "type": "recovery", "duration_sec": 240 }
      ] }
    ],
    "cooldown": { "duration_sec": 600 }
  }
}
```

### Le tool lui-même (`tools/garmin_tools.py`)

- Signature : `garmin_push_workout(date: str, name: str, structure: dict, dry_run: bool = True)`
- Ajoute à `GarminClient` les méthodes nécessaires (wrappers directs, même pattern `_call`) :
  `upload_workout(payload)`, `schedule_workout(workout_id, date_str)`,
  `get_scheduled_workouts(year, month)`, `delete_workout(workout_id)` (pour le nettoyage en cas
  d'échec partiel).
- Comportement :
  1. Si `dry_run` (défaut) : construit le payload via `workout_builder.build_workout(structure)`
     et le retourne tel quel, **aucun appel réseau d'écriture**. Output :
     `{ "status": "dry_run", "payload": {...} }`
  2. Sinon : vérifie d'abord via `get_scheduled_workouts` qu'aucune séance du même `name` n'est
     déjà programmée à cette `date` — si trouvée, ne repousse pas, renvoie son état existant
     plutôt qu'un doublon.
  3. `upload_workout(payload)` → récupère `workout_id`.
  4. `schedule_workout(workout_id, date)` — si ça échoue, retry 2-3 fois rapidement (le template
     existe déjà, pas besoin de re-upload). Si ça échoue encore, renvoie une erreur explicite
     incluant le `workout_id` orphelin plutôt que de le laisser silencieusement non programmé.
  5. Succès : `{ "status": "ok", "workout_id": <int>, "scheduled_date": "YYYY-MM-DD" }`
- N'oublie pas de logger l'appel comme les autres tools (le wrapper `_register_tool` de
  `server.py` s'en charge automatiquement si le tool est bien enregistré dans le dict retourné
  par `build_garmin_tools`).

---

## Tests attendus

Suis le pattern déjà en place dans `tests/test_garmin_tools.py` (mock de `GarminClient` entier
via `MagicMock(spec=GarminClient)`, jamais `garminconnect` en direct, DB SQLite en mémoire) :

- Tests pour `garmin_get_activity_weather`/`garmin_get_activity_details` : cache sans expiration
  (vérifier explicitement qu'un appel répété avec le même `activity_id` ne recontacte pas
  `GarminClient` même après un délai simulé > 1h — à la différence du comportement des autres
  tools `garmin_get_*`), gestion d'erreur standard.
- Nouveau fichier `tests/test_workout_builder.py` : tests purs sur `build_workout`/`_build_target`
  avec les 2 exemples fournis ci-dessus comme fixtures — en particulier le test explicite sur le
  sens de conversion allure→vitesse mentionné plus haut.
- Tests pour `garmin_push_workout` : dry_run par défaut (vérifie qu'aucune méthode d'écriture de
  `GarminClient` n'est appelée), anti-doublon (mock `get_scheduled_workouts` retournant une
  séance existante → pas de nouvel upload), retry sur échec de `schedule_workout`, erreur
  explicite avec `workout_id` si le retry échoue aussi.

## Definition of done

- [ ] `pytest` passe intégralement en local
- [ ] `garmin_push_workout` ne fait jamais d'appel d'écriture réel sans `dry_run=False` explicite
- [ ] Aucun fichier dans `deploy/` modifié, aucun commit/push vers un remote
- [ ] README mis à jour (section "Écarts volontaires...") si le format réel weather/details ou
  tout autre point a différé de ce brief
- [ ] Résumé final : ce qui a été fait, les écarts par rapport à ce brief et pourquoi, la
  commande exacte pour lancer les tests, et comment tester manuellement en local
  (`uvicorn src.server:app`) avant que l'utilisateur ne décide de déployer

Une fois ça fait, **arrête-toi** — ne déploie rien, ne pousse rien. L'utilisateur valide les
tests et fait un essai manuel en local avant toute mise en production.

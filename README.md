# mcp-garmin-coach

Serveur MCP distant exposant des données Garmin Connect et un état opérationnel (plan de
semaine, séance du jour, feedback) à un Project Claude.ai personnel de coaching triathlon.

Voir [`mcp-garmin-coach-spec.md`](../mcp-garmin-coach-spec.md) pour la spécification complète.

## Prérequis

- Python 3.11+
- Un compte Garmin Connect
- (déploiement) une VM avec Caddy en reverse proxy HTTPS — cf. `deploy/`

## Configuration (`.env`)

Copier `.env.example` vers `.env` et renseigner :

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `GARMIN_EMAIL` | Email du compte Garmin Connect |
| `GARMIN_PASSWORD` | Mot de passe du compte Garmin Connect |
| `MCP_SECRET_PATH` | Segment secret du chemin de montage (`/mcp/<secret>`). Générer avec la commande ci-dessous |
| `DB_PATH` | Chemin du fichier SQLite (défaut `./data/coach.db`) |
| `LOG_LEVEL` | Niveau de log (défaut `INFO`) |
| `PORT` | Port d'écoute HTTP local (défaut `8080`) |
| `PUBLIC_DOMAIN` | Domaine public déployé derrière Caddy (ex. `coach-ia.duckdns.org`, sans schéma ni port). Optionnel en local ; **requis en production** (cf. ci-dessous) |

Génération du secret :

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

`.env` contient des identifiants réels : il ne doit **jamais** être committé (déjà listé dans
`.gitignore`).

## Installation

```bash
python -m venv .venv
# Windows
.venv\Scripts\pip install -e ".[dev]"
# macOS/Linux
.venv/bin/pip install -e ".[dev]"
```

## Lancement en local

Depuis la racine du repo :

```bash
uvicorn src.server:app --host 127.0.0.1 --port 8080 --no-access-log
```

`--no-access-log` est important : le log d'accès par défaut d'uvicorn loggue le chemin complet
de chaque requête (donc le secret). Le logging HTTP applicatif (méthode + code retour, sans le
chemin) est assuré par `AccessLogMiddleware` dans `src/auth.py`.

Le serveur crée automatiquement les tables SQLite (`db.py`) et le dossier `data/` si absents,
puis répond sur `http://127.0.0.1:8080/mcp/<MCP_SECRET_PATH>`. Toute autre URL renvoie 404.

Le premier appel à un tool `garmin_*` déclenche la connexion à Garmin Connect (identifiants +
MFA si activé) ; le token de session est ensuite mis en cache localement (dossier
`<dossier de DB_PATH>/garmin_tokens/`) pour éviter de se reconnecter à chaque appel.

## Tests

```bash
pytest
```

Les tests mockent entièrement `garminconnect` et utilisent une base SQLite temporaire (en
mémoire) : aucun test n'appelle le vrai compte Garmin. La connexion à un vrai compte Garmin
Connect (via `garmin_sync`) doit être vérifiée manuellement une fois le serveur déployé.

## Fuseau horaire

Toutes les dates/heures générées côté serveur — dates métier (`today`, `week_start_date`,
dates par défaut des tools) **et** timestamps (`synced_at` de `garmin_sync`, `created_at` de
`sessions`/`activity_feedback`, `fetched_at` de `garmin_cache`) — sont en **Europe/Paris**,
indépendamment du fuseau du serveur (spec section 5bis). Point d'entrée unique :
`now_paris()` / `today_paris()` dans `src/models.py` — aucun `datetime.now()` naïf ni
`datetime.now(timezone.utc)` ailleurs dans le code. Seules les dates fournies en entrée par
l'utilisateur (paramètre `date` des tools) restent au format `YYYY-MM-DD` tel quel, sans
conversion.

En production, fixer aussi `TZ=Europe/Paris` au niveau du service systemd (déjà fait dans
`deploy/coach-mcp.service`) pour que les logs et l'horloge système soient cohérents avec le
code applicatif.

## Déploiement

Voir `deploy/coach-mcp.service` (unit systemd, chemins à adapter) et `deploy/Caddyfile.example`
(reverse proxy HTTPS de référence). La configuration réelle de la VM est décrite dans le guide
d'hébergement séparé.

**`PUBLIC_DOMAIN` est requis en production.** Le SDK `mcp` protège par défaut les endpoints
Streamable HTTP contre le DNS rebinding en validant les en-têtes `Host`/`Origin` des requêtes,
et n'autorise que `localhost`/`127.0.0.1` si aucun domaine n'est configuré explicitement
(`src/auth.py:build_transport_security`). Une fois déployé derrière Caddy sur un vrai domaine,
sans `PUBLIC_DOMAIN` renseigné dans `.env`, toute requête légitime reçoit **421 "Invalid Host
header"**. Renseigner `PUBLIC_DOMAIN=coach-ia.duckdns.org` (sans `https://` ni port) dans le
`.env` de la VM résout le problème ; `localhost`/`127.0.0.1` restent autorisés en parallèle, donc
les tests en local continuent de fonctionner sans configurer cette variable.

## Écarts volontaires par rapport à la spec initiale

À la demande de l'utilisateur, plusieurs tools s'écartent du schéma exact de
`mcp-garmin-coach-spec.md` (section 7), validés en conditions réelles :

- **`garmin_sync`** (7.1) : `synced_at` est en **Europe/Paris**, pas en UTC comme indiqué
  littéralement dans la spec (`"synced_at": "<ISO8601 UTC>"`) — corrigé pour respecter la
  contrainte plus générale de la section 5bis (tout timestamp généré côté serveur en
  Europe/Paris), qui prime sur la mention UTC de 7.1.
- **`garmin_get_recent_activities`** (7.7) : chaque activité expose des champs
  supplémentaires en plus du schéma d'origine.
  - Général : `garmin_type` (type brut Garmin, ex. `trail_running` vs `running`),
    `effet_entrainement` (`trainingEffectLabel` Garmin, ex. `TEMPO`,
    `LACTATE_THRESHOLD`, `AEROBIC_BASE`, `RECOVERY`), ainsi que le détail chiffré :
    `effet_aerobie`/`effet_aerobie_message` et
    `effet_anaerobie`/`effet_anaerobie_message` (scores 0-5 Garmin + message qualitatif,
    ex. `4.3` / `HIGHLY_IMPROVING_LACTATE_THRESHOLD_13`). `null` si absent.
  - Course (`type == "course"`) : `denivele_m`, `allure_moyenne_min_km`,
    `allure_rapide_min_km` (dérivée de `maxSpeed` ; Garmin n'expose pas d'allure "la
    plus lente" au niveau résumé d'activité). `null` pour les autres disciplines.
  - Nage (`type == "nage"`) : `piscine_longueur_m`, `nb_longueurs`, `swolf_moyen`,
    `cadence_moyenne_brasses_min`, `brasses_moyenne_longueur`, `brasses_total`,
    `meilleur_100m_sec`, `allure_moyenne_min_100m` (dérivée de `averageSpeed`,
    convention natation en min/100m plutôt que min/km). `null` pour les autres
    disciplines.
- **`log_activity_feedback`** (7.17) : `ressenti: str` devient `smiley: str` (emoji, non
  vide) et `note` passe d'un texte libre optionnel à un entier obligatoire 0-10. Le
  schéma SQLite (section 6) n'a pas changé : `smiley` est stocké dans la colonne
  `ressenti`, `note` (converti en texte) dans la colonne `note`.

Deux ajouts supplémentaires, hors des 17 tools de la spec initiale :

- **`garmin_get_training_load`** (7.8) : ajout de `cible_charge_chronique_min`/
  `cible_charge_chronique_max` (marge de charge chronique cible Garmin, ex. `240.8` à
  `451.5`) et `acwr_pourcentage`/`acwr_statut` (statut ACWR calculé par Garmin lui-même,
  ex. `OPTIMAL` à 57% — distinct de `load_status`, calculé par ce serveur selon la
  formule de la spec). `null` si absent.
- **`garmin_get_lactate_threshold`** (7.9) : la forme réelle de la réponse Garmin diffère
  de ce qui avait été supposé initialement — corrigée et validée contre un vrai compte.
  - `speed_and_heart_rate.speed` est à une échelle 10x inférieure au m/s standard (brut
    `0.369` → ×10 → `3.69 m/s` → `4:31/km`, conforme à l'allure affichée dans l'app
    Garmin).
  - `power` ne concerne qu'un seul sport à la fois (`RUNNING` ou `CYCLING`, jamais les
    deux) — routé vers `running` ou `cycling` selon le champ `sport`.
  - Ajout de `running.power_watts` et `running.power_watts_per_kg` (`functionalThresholdPower`
    et `powerToWeight` quand `sport == "RUNNING"`).
- **`garmin_get_training_load_balance`** (18ᵉ tool) : répartition mensuelle de charge
  aérobie faible / aérobie élevée / anaérobique ("Training Load Focus" Garmin), avec les
  cibles associées et un feedback qualitatif (ex. `ANAEROBIC_SHORTAGE`). Basé sur
  `mostRecentTrainingLoadBalance`, une donnée nichée dans la même réponse que
  `get_training_status`.
- **Repli sur les derniers jours avec données** pour `garmin_get_training_status`,
  `garmin_get_training_load`, `garmin_get_training_load_balance` et `garmin_get_vo2max` :
  Garmin ne recalcule
  ces métriques qu'après une activité synchronisée, donc "aujourd'hui" est très souvent
  vide. Ces trois tools remontent maintenant jusqu'à 7 jours en arrière pour trouver la
  dernière donnée disponible, et renvoient la **date réelle des données** (champ `date`
  du résultat) plutôt que de mélanger silencieusement une donnée ancienne avec la date du
  jour.

Quatre tools supplémentaires (hors des 18 tools ci-dessus), ajoutés et validés en conditions
réelles :

- **`garmin_get_activity_weather`** : `temp`/`apparentTemp`/`dewPoint` sont exposés par
  `garminconnect` en **°F**, pas en °C — repéré car `66` pour une soirée d'août pluvieuse à
  Paris serait absurde en Celsius (`66°C`) mais cohérent en Fahrenheit (`18.9°C`). Convertis
  en Celsius (`temp_celsius`, `apparent_temp_celsius`, `dew_point_celsius`). En revanche
  `windSpeed`/`windGust` n'ont pas de valeur assez caractéristique pour trancher leur unité
  (mph vs km/h plausibles tous les deux) — laissés bruts (`wind_speed_raw`, `wind_gust_raw`)
  plutôt que de deviner une conversion non vérifiée.
- **`garmin_get_activity_details`** : la forme brute confirme le couple
  `metricDescriptors`/`activityDetailMetrics` supposé dans le brief, avec un piège non
  anticipé : **l'ordre de la liste `metricDescriptors` ne correspond pas à l'ordre réel des
  valeurs dans le tableau `metrics` de chaque point** — seul le champ `metricsIndex` de
  chaque descripteur fait foi (ex. `directRunCadence` est listé en premier mais son
  `metricsIndex` réel est `4`). Le parsing construit une table `clé -> metricsIndex` à partir
  des descripteurs avant d'indexer, plutôt que de supposer un ordre. Par ailleurs
  `directSpeed` (vitesse brute) est absent sur l'activité utilisée pour valider ce parsing —
  repli sur `directGradeAdjustedSpeed` (vitesse ajustée au dénivelé), seul champ de vitesse
  disponible dans ce cas ; à surveiller si `directSpeed` s'avère présent sur d'autres types
  d'activité.
- **`garmin_push_workout`** : validé en conditions réelles (`dry_run=False`) après
  l'implémentation initiale — `upload_workout` et `schedule_workout` ont fonctionné du
  premier coup avec le payload produit par `workout_builder.build_workout`, et
  `delete_workout` a bien nettoyé la séance de test ensuite. La forme réelle de
  `get_scheduled_workouts` est `{"calendarItems": [...]}`, chaque item ayant un champ
  `title` (pas `workoutName`) et `date`. **Piège confirmé sur un vrai compte** :
  `calendarItems` mélange les activités déjà réalisées (`itemType: "activity"`) et les
  séances programmées — sans filtrage, une activité passée portant par coïncidence le même
  nom/date qu'une séance à programmer serait prise à tort pour un doublon et bloquerait le
  push. `_find_duplicate_scheduled_workout` (`src/tools/garmin_tools.py`) ignore désormais
  les entrées `itemType == "activity"`. Point non exercé : le vrai chemin "doublon détecté"
  n'a pas été déclenché en conditions réelles (aucune séance programmée en conflit au moment
  du test) — son comportement exact (`itemType` d'une vraie séance programmée non encore
  réalisée, notamment) reste une hypothèse raisonnable plutôt qu'une certitude.
  Autre hypothèse non explicitement tranchée par le brief : la numérotation `stepOrder`
  (`src/workout_builder.py`) est **séquentielle globale** sur tout le workout, y compris à
  l'intérieur d'un bloc répété (pas de redémarrage à 1 par groupe). Le vrai push a été
  accepté par Garmin avec ce choix, mais rien ne garantit que ce soit la convention exacte
  attendue par l'app/la montre plutôt qu'une simple tolérance de l'API à l'upload.
- **`garmin_delete_workout`** (hors périmètre initial du brief) : ajouté après coup, wrapper
  fin de `GarminClient.delete_workout` — utile pour nettoyer une séance de test comme celle
  poussée par `garmin_push_workout` pendant la validation manuelle. Validé en conditions
  réelles (suppression confirmée via une relecture de `get_scheduled_workouts`).

## Limites connues

Les endpoints Garmin Connect utilisés pour la charge d'entraînement détaillée, le seuil
lactique et les minutes d'intensité (`garmin_get_training_load`, `garmin_get_lactate_threshold`,
`garmin_get_intensity_minutes`) reposent sur l'API privée non documentée de Garmin Connect. Le
parsing dans `src/garmin_client.py` est basé sur la structure connue de cette API à la date
d'écriture, mais Garmin peut la faire évoluer sans préavis. Après le premier `garmin_sync` en
conditions réelles, il est recommandé d'inspecter le contenu de `garmin_cache` (table SQLite)
pour confirmer que les champs sont bien peuplés, et d'ajuster le parsing si nécessaire.

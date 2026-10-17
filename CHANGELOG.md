# Changelog

Toutes les évolutions sont **additives** : aucune clé de réponse existante n'est renommée ni
retirée (vérifié par `tests/test_non_regression.py`, schémas figés le 17/10/2026), sauf
l'exception signalée au 17/10/2026 (matin) pour `garmin_get_training_status`.

## 2026-10-17 (fin d'après-midi) — Analyse de natation côté serveur

### Ajouté
- **`garmin_analyse_swim(activity_id, css_blocks=None, target_css_min_per_100m=None)`** :
  agrégats uniquement (aucune liste de longueurs). `overview` (distance, longueurs, temps
  nagé, temps écoulé de la 1re longueur au dernier mur, repos, allure sur le temps nagé),
  `blocks` (temps nagé, allure, allures 1re/2e moitié, `drift_pct`, FC moyenne/max à titre
  informatif, SWOLF, repos après), `suspect_lengths` (écart à la médiane du bloc supérieur à
  max(3 × 1,4826 × MAD, 30 % de la médiane), blocs de 4 longueurs et plus), `css` (premier
  400 m puis premier 200 m qui le suit, ou `css_blocks` imposés ; sensibilité au court 5 s
  plus rapide ; `control.short_not_faster` ; écart à la cible sur 100 m et sur 1900 m) ou
  `null` avec `css_null_reason`. Erreurs au format `error: {type, message}`.
- **`garmin_get_swim_splits`** : par bloc, `swim_time_s`, `rest_after_s`,
  `first_half_pace_mmss`, `second_half_pace_mmss` (champs existants conservés).

### Validé sur données réelles
- Test CSS du 17/10 (24752973255) : 400 m en 6:45, 200 m en 3:20, CSS 1:42/100 m,
  sensibilité 1:45, `short_not_faster = true`, aucune longueur suspecte.
- 24748453127 : la règle MAD signale les longueurs 21 (18,0 s) et 22 (36,1 s, sa jumelle :
  virage mal détecté). La longueur 59 (20,3 s) reste sous le seuil de la règle dans son bloc
  (écart 8,45 s pour un seuil de 9,12 s) ; l'ancienne règle de `garmin_get_swim_splits`
  (`suspect`, −20 %/+40 %) continue de la signaler.

## 2026-10-17 (après-midi) — Fiabilisation de l'accès Garmin

### Ajouté
- **`garmin_health_check`** au format complet, quelle que soit l'issue (ne lève jamais) :
  `status`, `error` (`{type, message}` ou `null`), `checked_at`, `last_sync`
  (`at`, `age_minutes`, `source`, `device`), `latest_activity` (`{id, start, type}` ou `null`),
  `data_availability` (`activities` : ok/empty/error ; `sleep`, `body_battery`, `resting_hr` :
  ok/zero/empty/error).
- **Erreurs typées** sur toutes les réponses d'erreur : `error_type` (`auth`, `network`,
  `rate_limit`, `unknown`, `invalid_input`) et `error_detail: {type, message}`.
- **`last_sync`** (dernière synchro de la montre vers Garmin Connect) dans `garmin_sync`,
  `garmin_get_recent_activities` et `garmin_get_daily_snapshot`.
- **`availability`** (ok/zero/empty) sur sommeil, Body Battery et FC de repos ; dans le
  snapshot, `availability` par bloc et récapitulatif `data_availability`.
- **Body Battery** : `min_at`, `max_at`, `at_wake_at`.
- **`garmin_analyse_activity`** : `elapsed_s`, `moving_s`, `pauses` en tête de réponse ;
  `variation_fc_60s_bpm` (signée) et `variation_fc_60s` (« −12 bpm », « +3 bpm ») ; alias
  `pace_min_per_km` / `pace_mmss`.
- **Pauses** : clés `start_s`, `end_s`, `duration_s` (en plus de `debut_s`, `fin_s`, `duree_s`).
- **`garmin_get_swim_splits`** : `role` de chaque bloc (échauffement / série / retour au calme),
  `repos_avant_s`, `pace_min_per_100m` / `pace_mmss`, `pace_mmss` par longueur, `totaux`
  (temps de nage hors repos, temps de repos, total, distance).
- **Météo** : `wind_speed_unit` (« km/h »). **Activités course** : alias `pace_min_per_km` /
  `pace_mmss`.

### Modifié
- Une valeur à 0 pour le sommeil, la Body Battery (tous les points à 0) ou la FC de repos est
  traitée comme une absence de donnée : `status: "no_data"`, valeurs `null`,
  `availability: "zero"` (auparavant, une FC de repos ou une Body Battery à 0 sortait en `ok`).
- Erreurs HTTP 401/403 de garminconnect classées `auth` (et non plus « injoignable »).

### Écart volontaire à la demande
- La demande prévoyait `error.type` / `error.message` dans tous les outils. Or `error` y est
  déjà un booléen (`true`), lu par les clients et par le serveur : le transformer en objet
  aurait été un renommage de type. Le détail est donc dans `error_type` et
  `error_detail: {type, message}` ; seul `garmin_health_check`, qui n'avait pas de clé
  `error`, expose `error: {type, message}`.
- La FC de repos n'a pas d'heure de mesure chez Garmin (valeur journalière, `calendarDate`
  seul) : l'horodatage disponible est `measured_on` + `last_sync_at`.

## 2026-10-17 (matin)

- `garmin_get_activity_feedback` : sensations et effort perçu saisis sur la montre.
- `garmin_analyse_activity`, `garmin_get_daily_snapshot`, `garmin_health_check` (première
  version), `status` ok/no_data/error, `null` au lieu de 0, allures « M:SS », météo en km/h
  avec distance de la station, blocs et longueurs suspectes en natation.
- Seul changement non rétrocompatible : dans `garmin_get_training_status`, le libellé Garmin
  est passé de `status` à `training_status`.

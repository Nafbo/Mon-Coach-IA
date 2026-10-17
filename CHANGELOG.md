# Changelog

Toutes les évolutions sont **additives** : aucune clé de réponse existante n'est renommée ni
retirée (vérifié par `tests/test_non_regression.py`, schémas figés le 17/10/2026), sauf
l'exception signalée au 17/10/2026 (matin) pour `garmin_get_training_status`.

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

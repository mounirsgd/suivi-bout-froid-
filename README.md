# Suivi Bout Froid

Tableau de bord des temps de changement Bout Froid, alimente automatiquement
chaque matin depuis Firebase. Independant de l'application Gantt : aucun fichier
en commun, aucune ecriture dans la base.

Lien une fois en ligne : `https://mounirsgd.github.io/suivi-bout-froid/`

## Contenu

| Fichier | Role |
|---|---|
| `scripts/calcul_stats.py` | Lit Firebase, calcule les moyennes et les objectifs, ecrit `data/stats.json` |
| `.github/workflows/stats.yml` | Lance le calcul chaque jour a 08h07 (heure de Paris) |
| `index.html` | La page consultee par les utilisateurs |
| `data/stats.json` | Resultat du calcul. **Le fichier fourni contient des donnees de demonstration**, il sera remplace au premier calcul reel. |

## Mise en place

### 1. Creer le depot

Sur GitHub, nouveau depot **public** nomme `suivi-bout-froid`, puis deposer les
quatre fichiers en respectant l'arborescence.

### 2. Recuperer la cle Firebase

Console Firebase > projet `gantt-sgd` > Parametres du projet > Comptes de service
> **Generer une nouvelle cle privee**. Un fichier `.json` est telecharge.

### 3. Enregistrer la cle comme secret

Depot `suivi-bout-froid` > Settings > Secrets and variables > Actions >
**New repository secret**

- Nom : `FIREBASE_SERVICE_ACCOUNT`
- Valeur : tout le contenu du fichier `.json`, colle tel quel

La cle n'apparait jamais dans le code ni dans les journaux d'execution.

### 4. Activer GitHub Pages

Settings > Pages > Source : `Deploy from a branch`, branche `main`, dossier `/ (root)`.

### 5. Premier calcul

Onglet Actions > **Calcul des stats Bout Froid** > `Run workflow`. Le calcul
prend moins d'une minute et publie `data/stats.json`. La page est alors a jour.

## Regles de calcul

**Donnees lues.** Uniquement le premier creneau de chaque tache (`sh`, `sm`,
`eh`, `em`), comme l'export vers Power BI. Les creneaux 2 a 4 sont ignores.
Si l'heure de fin est anterieure a l'heure de debut, un jour est ajoute.

**Numero de ligne.** Extrait des chiffres du champ `machine`. Deux chiffres
recoivent le prefixe `2` : `Machine 32A`, `32B`, `232 A/B` donnent tous `232`.

**Indicateurs.**

| Code | Indicateur | Source |
|---|---|---|
| T0 | Temps vide de ligne | duree de `T0 : Nettoyage de ligne` |
| T1 | Temps pre-reglage | duree de `T1 : Duree pre-reglage` |
| T2 | Temps de fabrication de 2 lots | debut de `Top qualite` a fin de `Validation de deux lots commercialisables` |

**Objectifs.** Calcules sur la periode fixe du 1er juin a aujourd'hui,
independamment du filtre choisi dans la page. Pour chaque ligne, on trie ses
durees et on fait la moyenne des plus rapides :

- T0 : la moitie des valeurs, arrondie a l'entier inferieur
- T1 : les dix valeurs les plus rapides
- T2 : pas d'objectif pour l'instant

Une ligne sans donnee n'a pas d'objectif et apparait sans trait vert.

## Quand l'application Gantt change

Le script depend de quatre identifiants de taches et de la structure des
sessions Firebase :

```
bf_5  T0 : Nettoyage de ligne
bf_2  T1 : Duree pre-reglage
bf_6  Top qualite
bf_8  Validation de deux lots commercialisables
```

Renommer une tache est sans effet. Changer son identifiant, la structure de
`ganttData.tasks` ou le format du champ `machine` demande une mise a jour de
`scripts/calcul_stats.py`.

## Horaire

Le cron est regle sur `07 06 * * *` UTC, soit 08h07 a Paris en heure d'ete et
07h07 en heure d'hiver. GitHub declenche les taches planifiees en differe :
un retard de quelques minutes a une demi-heure est normal. Le bouton
`Run workflow` permet de relancer le calcul immediatement.

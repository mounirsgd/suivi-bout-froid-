#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Calcul des statistiques Bout Froid a partir de Firebase.

Lecture seule sur /sessions. Aucune ecriture dans Firebase.
Reproduit a l'identique la logique d'export de l'application Gantt :
  - creneau 1 uniquement (sh/sm/eh/em), les creneaux 2 a 4 sont ignores
  - correction du passage minuit : si fin < debut, on ajoute 1 jour
  - numero de ligne extrait des chiffres du champ "machine"

Sortie : data/stats.json
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, date

import firebase_admin
from firebase_admin import credentials, db

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

DATABASE_URL = "https://gantt-sgd-default-rtdb.europe-west1.firebasedatabase.app"

# Debut de la periode de reference pour le calcul des targets (fixe)
TARGET_DEBUT = date(2026, 6, 1)

# Fichier de sortie
SORTIE = os.path.join("data", "stats.json")

# Identifiants des taches Bout Froid utilisees (cf. TASKS_BOUT_FROID dans app.js)
TACHE_T0 = "bf_5"   # T0 : Nettoyage de ligne
TACHE_T1 = "bf_2"   # T1 : Duree pre-reglage
TACHE_TQ = "bf_6"   # Top qualite            -> debut du calcul T2
TACHE_VAL = "bf_8"  # Validation de deux lots -> fin du calcul T2

# Libelles affiches dans le dashboard
METRIQUES = {
    "T0": "Temps vide de ligne",
    "T1": "Temps pre-reglage",
    "T2": "Temps de fabrication de 2 lots commercialisables",
}

# Regles de calcul des targets :
#   "moitie"  -> on garde la moitie des valeurs les plus rapides (arrondi bas)
#   ("top", N) -> on garde les N valeurs les plus rapides
REGLES_TARGET = {
    "T0": "moitie",
    "T1": ("top", 10),
    # T2 : regle non definie pour l'instant, pas de target calculee
}


# ─────────────────────────────────────────────────────────────────────────────
# OUTILS
# ─────────────────────────────────────────────────────────────────────────────

def normaliser_ligne(machine):
    """
    Extrait le numero de ligne normalise depuis le champ texte libre "machine".

    "Machine 32A"     -> "232"
    "32B"             -> "232"
    "Machine 232"     -> "232"
    "232 A/B"         -> "232"
    "Machine 21"      -> "221"
    "Machine 232 - X" -> "232"   (on ne prend que le premier groupe de chiffres)

    Retourne None si aucun chiffre n'est trouve.
    """
    if not machine:
        return None
    trouve = re.search(r"\d+", str(machine))
    if not trouve:
        return None
    chiffres = trouve.group()
    if len(chiffres) == 2:
        # forme courte : on rajoute le prefixe "2" comme dans Power BI
        return "2" + chiffres
    if len(chiffres) == 3:
        return chiffres
    # longueur inattendue (1 chiffre, ou 4 et plus) : on ne devine pas
    return None


def heure_vers_minutes(h, m):
    """Convertit une paire (heure, minute) en minutes depuis minuit. None si vide."""
    if h is None or m is None:
        return None
    h, m = str(h).strip(), str(m).strip()
    if h == "" or m == "":
        return None
    try:
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def horodatage(jour_str, minutes, decalage_jour=0):
    """Construit un datetime a partir d'une date ISO et d'un nombre de minutes."""
    if minutes is None:
        return None
    try:
        base = datetime.strptime(jour_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return base + timedelta(days=decalage_jour, minutes=minutes)


def lire_creneau(tache):
    """
    Lit le creneau 1 d'une tache et retourne (debut_min, fin_min).
    Les creneaux 2 a 4 sont volontairement ignores (comme a l'export).
    """
    if not isinstance(tache, dict):
        return None, None
    debut = heure_vers_minutes(tache.get("sh"), tache.get("sm"))
    fin = heure_vers_minutes(tache.get("eh"), tache.get("em"))
    return debut, fin


def duree_minutes(debut_min, fin_min):
    """Duree en minutes, avec correction du passage minuit."""
    if debut_min is None or fin_min is None:
        return None
    duree = fin_min - debut_min
    if duree < 0:
        duree += 1440
    return duree


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def connecter_firebase():
    """Initialise Firebase en lecture seule via la cle de compte de service."""
    cle_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not cle_json:
        sys.exit("Erreur : le secret FIREBASE_SERVICE_ACCOUNT est absent.")
    try:
        infos = json.loads(cle_json)
    except json.JSONDecodeError:
        sys.exit("Erreur : FIREBASE_SERVICE_ACCOUNT n'est pas un JSON valide.")
    cred = credentials.Certificate(infos)
    firebase_admin.initialize_app(cred, {"databaseURL": DATABASE_URL})


def charger_sessions():
    """Recupere toutes les sessions depuis Firebase."""
    sessions = db.reference("sessions").get()
    return sessions or {}


def extraire_mesures(sessions):
    """
    Transforme les sessions brutes en une liste plate de mesures :
        {"date": "2026-09-14", "ligne": "221", "metrique": "T0", "duree_min": 33}
    """
    mesures = []

    for _, session in sessions.items():
        if not isinstance(session, dict):
            continue

        jour = session.get("date")
        ligne = normaliser_ligne(session.get("machine"))
        if not jour or not ligne:
            continue

        taches = (session.get("ganttData") or {}).get("tasks") or {}

        # ── T0 et T1 : duree simple de la tache ──────────────────────────────
        for code, id_tache in (("T0", TACHE_T0), ("T1", TACHE_T1)):
            debut, fin = lire_creneau(taches.get(id_tache))
            duree = duree_minutes(debut, fin)
            if duree is not None and duree > 0:
                mesures.append({
                    "date": jour,
                    "ligne": ligne,
                    "metrique": code,
                    "duree_min": duree,
                })

        # ── T2 : de Top qualite (debut) a Validation 2 lots (fin) ────────────
        debut_tq, _ = lire_creneau(taches.get(TACHE_TQ))
        debut_val, fin_val = lire_creneau(taches.get(TACHE_VAL))

        depart = horodatage(jour, debut_tq)
        # la fin de Validation peut basculer au lendemain (passage minuit)
        decalage = 1 if (debut_val is not None and fin_val is not None
                         and fin_val < debut_val) else 0
        arrivee = horodatage(jour, fin_val, decalage)

        if depart and arrivee and arrivee > depart:
            ecart = (arrivee - depart).total_seconds() / 60
            mesures.append({
                "date": jour,
                "ligne": ligne,
                "metrique": "T2",
                "duree_min": round(ecart),
            })

    mesures.sort(key=lambda m: (m["date"], m["ligne"], m["metrique"]))
    return mesures


# ─────────────────────────────────────────────────────────────────────────────
# TARGETS
# ─────────────────────────────────────────────────────────────────────────────

def calculer_targets(mesures, aujourdhui):
    """
    Calcule la target de chaque ligne, par metrique.

    Periode fixe : du 1er juin a aujourd'hui, independante des filtres
    du dashboard. On garde les valeurs les plus rapides selon la regle
    de la metrique, puis on en fait la moyenne.
    """
    targets = {}

    for code, regle in REGLES_TARGET.items():
        par_ligne = {}

        for mesure in mesures:
            if mesure["metrique"] != code:
                continue
            try:
                jour = datetime.strptime(mesure["date"], "%Y-%m-%d").date()
            except ValueError:
                continue
            if not (TARGET_DEBUT <= jour <= aujourdhui):
                continue
            par_ligne.setdefault(mesure["ligne"], []).append(mesure["duree_min"])

        resultat = {}
        for ligne, valeurs in par_ligne.items():
            valeurs.sort()
            if regle == "moitie":
                garde = len(valeurs) // 2
            else:
                garde = min(regle[1], len(valeurs))
            # si le calcul ne retient rien, on retombe sur la valeur unique
            if garde < 1:
                garde = len(valeurs)
            if garde < 1:
                continue
            retenues = valeurs[:garde]
            resultat[ligne] = round(sum(retenues) / len(retenues))

        targets[code] = resultat

    # metriques sans regle definie : target vide
    for code in METRIQUES:
        targets.setdefault(code, {})

    return targets


# ─────────────────────────────────────────────────────────────────────────────
# PROGRAMME PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

def main():
    aujourdhui = date.today()

    connecter_firebase()
    sessions = charger_sessions()
    print("Sessions lues : %d" % len(sessions))

    mesures = extraire_mesures(sessions)
    print("Mesures extraites : %d" % len(mesures))

    targets = calculer_targets(mesures, aujourdhui)
    for code, valeurs in targets.items():
        print("Target %s : %d ligne(s)" % (code, len(valeurs)))

    lignes = sorted({m["ligne"] for m in mesures})

    sortie = {
        "derniere_maj": datetime.now().isoformat(timespec="seconds"),
        "periode_target": {
            "debut": TARGET_DEBUT.isoformat(),
            "fin": aujourdhui.isoformat(),
        },
        "metriques": METRIQUES,
        "lignes": lignes,
        "targets": targets,
        "mesures": mesures,
    }

    os.makedirs(os.path.dirname(SORTIE), exist_ok=True)
    with open(SORTIE, "w", encoding="utf-8") as f:
        json.dump(sortie, f, ensure_ascii=False, indent=1)

    print("Ecrit : %s" % SORTIE)


if __name__ == "__main__":
    main()

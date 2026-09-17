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

# Debut de la periode de reference pour le calcul des objectifs (fixe)
TARGET_DEBUT = date(2026, 6, 1)

SORTIE = os.path.join("data", "stats.json")

# Identifiants des taches Bout Froid (cf. TASKS_BOUT_FROID dans app.js)
TACHE_T0 = "bf_5"   # T0 : Nettoyage de ligne
TACHE_T1 = "bf_2"   # T1 : Duree pre-reglage
TACHE_TQ = "bf_6"   # Top qualite             -> debut de la fenetre T2
TACHE_VAL = "bf_8"  # Validation de deux lots -> fin de la fenetre T2

# Toutes les taches Bout Froid, dans l'ordre du formulaire.
# Sert a rassembler les commentaires pour T2, qui couvre toute la sequence.
TACHES_BOUT_FROID = [
    ("bf_1",  "Aligneur vide"),
    ("bf_5",  "T0 : Nettoyage de ligne"),
    ("bf_2",  "T1 : Duree pre-reglage"),
    ("bf_4",  "Arrivee deux sections controlables"),
    ("bf_3",  "Arrivee de toutes sections"),
    ("bf_6",  "Top qualite"),
    ("bf_9",  "Montee en regime"),
    ("bf_10", "Premiere palette sortie"),
    ("bf_7",  "Premier lot sorti"),
    ("bf_11", "Top emballage"),
    ("bf_8",  "Validation de deux lots commercialisables"),
]

# Motifs proposes par l'application (CAUSES_BOUT_FROID_DICT dans app.js).
# Sert a distinguer un motif coche d'un texte saisi a la main.
MOTIFS_CONNUS = {
    "Manque de personnel", "Vide de l'arche BF", "Vide de la ligne entiere",
    "Vide de la ligne entière",
    "Nettoyage non termine", "Nettoyage non terminé",
    "Temoins NOK", "Témoins NOK", "Chariot film NOK", "Materiel NOK", "Matériel NOK",
    "Nettoyage (debris verre)", "Nettoyage (débris verre)",
    "Intervention maintenance", "Reglage non termine", "Réglage non terminé",
    "Creation fiche", "Création fiche", "Formation",
    "Retard arrivee sections", "Retard arrivée sections", "Recuit NOK",
    "Reglage equipement BF", "Réglage équipement BF",
    "Retard demarrage", "Retard démarrage", "Top Qualite retarde", "Top Qualité retardé",
    "Top Emballage retarde", "Top Emballage retardé",
    "Tombees sur arche", "Tombées sur arche", "Reglage BF", "Réglage BF",
    "Demarrage tardif", "Démarrage tardif", "SAP",
    "Cadence non atteinte", "Probleme mecanique", "Problème mécanique",
    "Probleme reglage", "Problème réglage",
    "Lot bloque", "Lot bloqué", "Defaut qualite", "Défaut qualité",
    "Defaut palettisation", "Défaut palettisation", "SAP - etiquette", "SAP - étiquette",
    "Validation retardee", "Validation retardée",
}
MOTIFS_NORMALISES = {m.lower() for m in MOTIFS_CONNUS}

# Formules signalant qu'il n'y a rien a signaler
FORMULES_RAS = {"ras", "r.a.s", "rien a signaler", "rien à signaler",
                "neant", "néant", "aucun", "aucune"}

# Reperage des simples releves d'horaires et de numeros de lots
_HEURE = re.compile(r"\d{1,2}\s*[h:]\s*\d{2}")
_LOT = re.compile(r"\blots?\b\s*\d|\bM\d{2,}\b")
_MOTS_POINTAGE = {
    "a", "à", "de", "des", "du", "le", "la", "les", "et", "en", "au", "aux", "sur",
    "top", "qualite", "qualité", "lot", "lots", "premier", "premiers", "deux",
    "sections", "section", "toutes", "tous", "arche", "emballage", "sorti",
    "sortie", "livrables", "commercialisables", "validation", "h", "min",
    "palette", "premiere", "première", "vide", "aligneur", "demarrage", "départ",
}


def classer_cause(texte):
    """
    Nature d'un morceau de commentaire :
      "motif"  : coche dans la liste de l'application
      "ras"    : rien a signaler
      "releve" : simple releve d'heure ou de numero de lot, pas une cause
      "libre"  : cause reelle, ecrite a la main
    """
    s = (texte or "").strip()
    if not s:
        return "releve"

    if s.lower() in MOTIFS_NORMALISES:
        return "motif"
    if s.lower().strip(".") in FORMULES_RAS:
        return "ras"

    if _HEURE.search(s) or _LOT.search(s):
        reste = _HEURE.sub(" ", s)
        reste = re.sub(r"[0-9()/\-=,.:;+]", " ", reste)
        mots = [m for m in re.split(r"\s+", reste.lower()) if m]
        utiles = [m for m in mots if m not in _MOTS_POINTAGE and len(m) > 2]
        if not utiles:
            return "releve"

    return "libre"


LIBELLES = {
    "T0": "T0 : Nettoyage de ligne",
    "T1": "T1 : Duree pre-reglage",
}

METRIQUES = {
    "T0": "Temps vide de ligne",
    "T1": "Temps pre-reglage",
    "T2": "Montée en régime",
}

# Sur T0 uniquement : une duree strictement inferieure a ce seuil correspond
# a un changement d'indice, pas a un vrai changement de ligne. Elle est
# ecartee partout (barres, objectif, tableau, evolution).
SEUIL_T0_MIN = 30

# Objectif : pour chaque ligne, on trie ses durees et on fait la moyenne
# de la moitie la plus rapide (arrondi a l'entier inferieur).
# Meme regle pour les trois indicateurs.
PART_RETENUE = 0.5


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
    "Machine 232 - X" -> "232"   (seul le premier groupe de chiffres compte)

    Retourne None si aucun chiffre exploitable.
    """
    if not machine:
        return None
    trouve = re.search(r"\d+", str(machine))
    if not trouve:
        return None
    chiffres = trouve.group()
    if len(chiffres) == 2:
        return "2" + chiffres      # forme courte : prefixe "2", comme Power BI
    if len(chiffres) == 3:
        return chiffres
    return None


def heure_vers_minutes(h, m):
    """Convertit une paire (heure, minute) en minutes depuis minuit."""
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
    Lit le creneau 1 d'une tache : (debut_min, fin_min, commentaire).
    Les creneaux 2 a 4 sont volontairement ignores, comme a l'export.
    """
    if not isinstance(tache, dict):
        return None, None, ""
    debut = heure_vers_minutes(tache.get("sh"), tache.get("sm"))
    fin = heure_vers_minutes(tache.get("eh"), tache.get("em"))
    commentaire = (tache.get("comment") or "").strip()
    return debut, fin, commentaire


def duree_minutes(debut_min, fin_min):
    """Duree en minutes, avec correction du passage minuit."""
    if debut_min is None or fin_min is None:
        return None
    duree = fin_min - debut_min
    if duree < 0:
        duree += 1440
    return duree


def decouper_causes(commentaire):
    """
    Decoupe un commentaire en causes elementaires.
    L'application enregistre les motifs coches et le texte libre dans le
    meme champ, separes par " | ". Motifs et texte libre sont traites de
    la meme facon : chaque morceau non vide compte pour une cause.
    """
    if not commentaire:
        return []
    return [p.strip() for p in commentaire.split("|") if p.strip()]


def extraire_causes(sessions):
    """
    Une entree par cause citee, pour le Pareto :
        {"date": "2026-09-14", "ligne": "221",
         "tache": "T0 : Nettoyage de ligne", "cause": "Manque de personnel"}
    Chaque commentaire n'est compte qu'une fois, sur sa propre tache.
    """
    causes = []
    vues = set()          # (date, ligne, cause) deja comptes
    for _, session in sessions.items():
        if not isinstance(session, dict):
            continue
        jour = session.get("date")
        ligne = normaliser_ligne(session.get("machine"))
        if not jour or not ligne:
            continue
        taches = (session.get("ganttData") or {}).get("tasks") or {}
        for id_tache, libelle in TACHES_BOUT_FROID:
            _, _, commentaire = lire_creneau(taches.get(id_tache))
            for cause in decouper_causes(commentaire):
                # une meme cause citee sur plusieurs taches du meme jour et
                # de la meme ligne ne compte qu'une fois
                cle = (jour, ligne, cause.lower())
                if cle in vues:
                    continue
                vues.add(cle)
                causes.append({
                    "date": jour,
                    "ligne": ligne,
                    "tache": libelle,
                    "cause": cause,
                    "type": classer_cause(cause),
                })
    causes.sort(key=lambda c: (c["date"], c["ligne"]))
    return causes


def details_bout_froid(taches):
    """
    Commentaires non vides de toutes les taches Bout Froid d'une session,
    sous forme de liste [{"tache": ..., "texte": ...}]. Utilise pour T2, qui
    couvre l'ensemble de la sequence : la cause peut venir de n'importe
    quelle etape intermediaire. La page les affiche tache par tache.
    """
    details = []
    for id_tache, libelle in TACHES_BOUT_FROID:
        _, _, commentaire = lire_creneau(taches.get(id_tache))
        if commentaire:
            details.append({"tache": libelle, "texte": commentaire})
    return details


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def connecter_firebase():
    """Initialise Firebase via la cle de compte de service."""
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
    """Recupere toutes les sessions. Lecture seule."""
    sessions = db.reference("sessions").get()
    return sessions or {}


def extraire_mesures(sessions):
    """
    Transforme les sessions brutes en une liste plate de mesures :
        {"date": "2026-09-14", "ligne": "221", "metrique": "T0",
         "duree_min": 33, "commentaire": "Manque de personnel"}
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

        # ── T0 et T1 : duree de la tache elle-meme ───────────────────────────
        for code, id_tache in (("T0", TACHE_T0), ("T1", TACHE_T1)):
            debut, fin, commentaire = lire_creneau(taches.get(id_tache))
            duree = duree_minutes(debut, fin)
            if duree is not None and duree > 0:
                # T0 : on ecarte les changements d'indice
                if code == "T0" and duree < SEUIL_T0_MIN:
                    continue
                mesures.append({
                    "date": jour,
                    "ligne": ligne,
                    "metrique": code,
                    "duree_min": duree,
                    "details": ([{"tache": LIBELLES[code], "texte": commentaire}]
                                if commentaire else []),
                })

        # ── T2 : du debut de Top qualite a la fin de Validation 2 lots ───────
        debut_tq, _, _ = lire_creneau(taches.get(TACHE_TQ))
        debut_val, fin_val, _ = lire_creneau(taches.get(TACHE_VAL))

        depart = horodatage(jour, debut_tq)
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
                "details": details_bout_froid(taches),
            })

    mesures.sort(key=lambda m: (m["date"], m["ligne"], m["metrique"]))
    return mesures


# ─────────────────────────────────────────────────────────────────────────────
# OBJECTIFS
# ─────────────────────────────────────────────────────────────────────────────

def calculer_targets(mesures, aujourdhui):
    """
    Objectif de chaque ligne, pour chaque indicateur.

    Periode fixe du 1er juin a aujourd'hui, independante des filtres de la
    page. On trie les durees de la ligne et on fait la moyenne de la moitie
    la plus rapide : 6 valeurs -> 3, 20 valeurs -> 10, 21 valeurs -> 10.
    Une seule valeur disponible : elle sert d'objectif.
    """
    targets = {}
    effectifs = {}

    for code in METRIQUES:
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
        compte = {}
        for ligne, valeurs in par_ligne.items():
            valeurs.sort()
            garde = int(len(valeurs) * PART_RETENUE)
            if garde < 1:
                garde = len(valeurs)       # une seule saisie : on la garde
            retenues = valeurs[:garde]
            resultat[ligne] = round(sum(retenues) / len(retenues))
            compte[ligne] = {"total": len(valeurs), "retenues": garde}

        targets[code] = resultat
        effectifs[code] = compte

    return targets, effectifs


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

    avec_commentaire = sum(1 for m in mesures if m["details"])
    print("Dont avec commentaire : %d" % avec_commentaire)

    causes = extraire_causes(sessions)
    from collections import Counter
    repartition = Counter(c["type"] for c in causes)
    retenues = [c for c in causes if c["type"] in ("motif", "libre")]
    print("Causes citees : %d" % len(causes))
    print("  motifs coches  : %d" % repartition["motif"])
    print("  causes libres  : %d" % repartition["libre"])
    print("  releves ecartes: %d" % repartition["releve"])
    print("  RAS            : %d" % repartition["ras"])
    print("  -> Pareto sur %d causes, %d distinctes"
          % (len(retenues), len({c["cause"] for c in retenues})))

    targets, effectifs = calculer_targets(mesures, aujourdhui)
    for code in METRIQUES:
        print("Objectif %s : %d ligne(s)" % (code, len(targets[code])))

    sortie = {
        "derniere_maj": datetime.now().isoformat(timespec="seconds"),
        "periode_target": {
            "debut": TARGET_DEBUT.isoformat(),
            "fin": aujourdhui.isoformat(),
        },
        "metriques": METRIQUES,
        "lignes": sorted({m["ligne"] for m in mesures}),
        "targets": targets,
        "effectifs": effectifs,
        "mesures": mesures,
        "causes": causes,
    }

    os.makedirs(os.path.dirname(SORTIE), exist_ok=True)
    with open(SORTIE, "w", encoding="utf-8") as f:
        json.dump(sortie, f, ensure_ascii=False, indent=1)

    print("Ecrit : %s" % SORTIE)


if __name__ == "__main__":
    main()

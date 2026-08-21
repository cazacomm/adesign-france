#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Génération automatique d'un article de blog — ADesign.

Le script :
  1. lit blog-config.json ;
  2. extrait de BLOG_WORKFLOW.md le tableau des sujets suggérés et les règles
     éditoriales ;
  3. scanne /blog/*/index.html pour savoir quels sujets sont déjà traités ;
  4. choisit le prochain sujet non traité (ordre séquentiel) ;
  5. relit l'article de référence pour s'en servir de gabarit HTML ;
  6. demande à l'API OpenAI le seul CONTENU éditorial, en JSON structuré
     (titre, chapô, sections h2/h3, paragraphes, listes, FAQ) ;
  7. valide ce contenu, puis ASSEMBLE lui-même la page : head, meta, canonical,
     Open Graph, Twitter Card, les trois blocs JSON-LD, le fil d'Ariane, le
     marqueur d'idempotence, le header et le footer viennent du gabarit et du
     script — jamais du modèle ;
  8. écrit /blog/<slug>/index.html, puis met à jour blog/index.html,
     sitemap.xml, blog/rss.xml et llms.txt.

Le modèle n'écrit donc pas une ligne de HTML. Quand il régénérait toute la page,
les deux tiers de ses tokens de sortie partaient en balisage, ce qui plafonnait
le corps rédigé autour de 850 mots quelle que soit la consigne.

Codes de sortie :
   0  succès
   1  erreur (rien n'a été écrit)
  78  aucun nouveau sujet à traiter (EX_CONFIG — arrêt propre)

Options :
  --dry-run       n'écrit aucun fichier, affiche le résultat
  --mock          n'appelle pas l'API (contenu de démonstration)
  --rewrite SLUG  régénère un article existant et écrase son fichier
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "blog-config.json"
WORKFLOW_PATH = ROOT / "BLOG_WORKFLOW.md"
BLOG_DIR = ROOT / "blog"
BLOG_INDEX = BLOG_DIR / "index.html"
SITEMAP = ROOT / "sitemap.xml"
RSS = BLOG_DIR / "rss.xml"          # ADesign héberge le flux dans /blog/
LLMS = ROOT / "llms.txt"

EXIT_OK, EXIT_ERROR, EXIT_NOTHING_TODO = 0, 1, 78

# Volume du corps rédigé, FAQ exclue, compté sur le contenu et non sur le HTML.
#  · PROMPT_MIN/MAX_WORDS : la cible, annoncée au modèle et seuil de rattrapage.
#  · MIN/MAX_WORDS        : bornes de validation, plus larges (tolérance ±30 %).
MIN_WORDS, MAX_WORDS = 900, 1900
PROMPT_MIN_WORDS, PROMPT_MAX_WORDS = 1200, 1500

# Nombre maximal d'appels OpenAI pour un article, rattrapages compris.
MAX_CALLS = 3

MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin",
             "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
DAYS_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Les 19 clés attendues dans blog-config.json.
REQUIRED_KEYS = (
    "site_name", "site_url", "sector", "location", "geo_keywords", "tone",
    "author", "target_word_count", "faq_questions_count", "language", "model",
    "temperature", "topic_marker_prefix", "og_image", "logo_path",
    "default_article_section", "internal_link_targets",
    "reference_article_slug", "facts",
)

STOPWORDS = {
    "le", "la", "les", "un", "une", "des", "du", "de", "d", "l", "et", "ou", "a", "au",
    "aux", "en", "dans", "sur", "pour", "par", "avec", "sans", "que", "qui", "quoi",
    "ce", "cet", "cette", "ces", "se", "sa", "son", "ses", "nos", "notre", "votre",
    "vos", "est", "ne", "pas", "plus", "tout", "tous", "toute", "toutes", "y", "il",
    "elle", "on", "vraiment", "bien",
}


# ─────────────────────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[blog] {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"[blog][ERREUR] {msg}", file=sys.stderr, flush=True)


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text)
                   if unicodedata.category(c) != "Mn")


def slugify(title: str, max_words: int = 7) -> str:
    """Slug déterministe : même titre => même slug (garantit l'idempotence)."""
    text = strip_accents(title.lower())
    text = text.replace("'", " ").replace("’", " ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [w for w in text.split() if w and w not in STOPWORDS]
    if not words:
        words = [w for w in text.split() if w]
    return "-".join(words[:max_words])


def esc(text: str) -> str:
    """Échappement HTML. Tout le contenu du modèle passe par là : il fournit du
    texte brut, jamais du markup, ce qui rend une injection HTML impossible."""
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def inline(text: str) -> str:
    """Rend le balisage inline autorisé dans le texte du modèle, après
    échappement : **gras** et [libellé](/chemin-interne).

    Les liens sont restreints aux chemins commençant par « / » : le maillage
    interne reste possible, un lien externe devient structurellement impossible."""
    out = esc(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"\[([^\]]+)\]\((/[^)\s]*)\)", r'<a href="\2">\1</a>', out)
    return out


def plain(text: str) -> str:
    """Texte débarrassé du balisage inline — pour les JSON-LD et les meta."""
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return re.sub(r"\[([^\]]+)\]\((/[^)\s]*)\)", r"\1", out)


def content_word_count(data: dict) -> int:
    """Volume rédactionnel du corps, FAQ exclue — compté sur le contenu lui-même
    et non sur du HTML : plus de balises ni de boilerplate dans le total."""
    words = len(plain(data.get("lede", "")).split())
    for section in data.get("sections", []):
        words += len(plain(section.get("h2", "")).split())
        for block in section.get("content", []):
            words += len(plain(block.get("text", "")).split())
            for item in block.get("items", []) or []:
                words += len(plain(item).split())
    return words


def fr_date(d: dt.date) -> str:
    return f"{d.day} {MONTHS_FR[d.month - 1]} {d.year}"


def rfc822(d: dt.date, hour: str = "09:00:00") -> str:
    return f"{DAYS_EN[d.weekday()]}, {d.day:02d} {MONTHS_EN[d.month - 1]} {d.year} {hour} +0200"


# ─────────────────────────────────────────────────────────────
# Lecture de la configuration et du workflow
# ─────────────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Configuration introuvable : {CONFIG_PATH}")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED_KEYS if not cfg.get(k)]
    if missing:
        raise ValueError("Clés manquantes ou vides dans blog-config.json : "
                         + ", ".join(missing))
    if len(cfg["internal_link_targets"]) < 3:
        raise ValueError("internal_link_targets doit contenir au moins 3 chemins.")
    cfg["site_url"] = cfg["site_url"].rstrip("/")
    return cfg


def parse_topics(workflow: str) -> list[dict]:
    """Extrait les sujets de la section « Sujets suggérés » de BLOG_WORKFLOW.md.

    ADesign présente ses sujets sous forme de TABLEAU markdown (| # | Sujet |
    Angle |) et non de liste numérotée : le parseur lit le tableau en priorité,
    et retombe sur la liste numérotée si le document change de forme. Le
    document lui-même n'est jamais modifié par le script.
    """
    m = re.search(r"^##\s+\d+\.\s+[^\n]*[Ss]ujets\s+sugg[ée]r[ée]s[^\n]*$(.*?)(?=^##\s|\Z)",
                  workflow, flags=re.M | re.S)
    if not m:
        raise ValueError("Section des sujets suggérés introuvable dans BLOG_WORKFLOW.md")
    block = m.group(1)

    topics: list[dict] = []

    # ── Forme 1 : tableau markdown ──
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2 or not cells[0].isdigit():
            continue                       # en-tête, séparateur ou ligne libre
        title = re.sub(r"[`*✅]", "", cells[1]).strip()
        brief = re.sub(r"[`*✅]", "", cells[2]).strip() if len(cells) > 2 else ""
        if not title:
            continue
        slug_m = re.search(r"`([a-z0-9\-]+)`", line)
        topics.append({
            "num": int(cells[0]),
            "title": title,
            "brief": brief,
            "declared_slug": slug_m.group(1) if slug_m else None,
            "declared_published": "publié" in line.lower(),
        })

    # ── Forme 2 (repli) : liste numérotée « 1. **Titre** — angle » ──
    if not topics:
        for num, line in re.findall(r"^(\d+)\.\s+(.*)$", block, flags=re.M):
            title_m = re.search(r"\*\*(.+?)\*\*", line)
            if not title_m:
                continue
            rest = line[title_m.end():].lstrip(" —-–").strip()
            slug_m = re.search(r"`([a-z0-9\-]+)`", line)
            topics.append({
                "num": int(num),
                "title": title_m.group(1).strip(),
                "brief": re.sub(r"[`*✅]", "", rest).strip(),
                "declared_slug": slug_m.group(1) if slug_m else None,
                "declared_published": "publié" in line.lower(),
            })

    if not topics:
        raise ValueError("Aucun sujet exploitable trouvé dans BLOG_WORKFLOW.md")
    topics.sort(key=lambda t: t["num"])
    return topics


def parse_editorial_rules(workflow: str) -> str:
    """Récupère les règles éditoriales pour les injecter dans le prompt.

    Chez ADesign la section s'intitule « Contenu — les règles à ne pas
    franchir » ; on accepte aussi le libellé « Règles éditoriales ».
    """
    for pattern in (r"^##\s+\d+\.\s+Contenu[^\n]*r[èe]gles[^\n]*$(.*?)(?=^##\s|\Z)",
                    r"^##\s+\d+\.\s+R[èe]gles\s+[ée]ditoriales[^\n]*$(.*?)(?=^##\s|\Z)",
                    r"^##\s+\d+\.\s+[^\n]*r[èe]gles[^\n]*$(.*?)(?=^##\s|\Z)"):
        m = re.search(pattern, workflow, flags=re.M | re.S | re.I)
        if m:
            return m.group(1).strip()
    return ""


# ─────────────────────────────────────────────────────────────
# État du blog
# ─────────────────────────────────────────────────────────────

def scan_blog(marker_prefix: str) -> tuple[set[int], set[str]]:
    """Retourne (numéros de sujets déjà traités, slugs existants)."""
    done_nums: set[int] = set()
    slugs: set[str] = set()
    if not BLOG_DIR.exists():
        return done_nums, slugs
    for path in sorted(BLOG_DIR.glob("*/index.html")):
        slug = path.parent.name
        slugs.add(slug)
        html = path.read_text(encoding="utf-8", errors="replace")
        m = re.search(rf"<!--\s*{re.escape(marker_prefix)}:\s*(\d+)\s*-->", html)
        if m:
            done_nums.add(int(m.group(1)))
    return done_nums, slugs


def pick_topic(topics: list[dict], done_nums: set[int], slugs: set[str]) -> dict | None:
    """Premier sujet non traité, dans l'ordre de la liste."""
    for topic in topics:
        if topic["num"] in done_nums:
            continue
        if topic["declared_slug"] and topic["declared_slug"] in slugs:
            continue
        slug = slugify(topic["title"])
        if slug in slugs:
            # Le dossier existe déjà : on considère le sujet traité (idempotence).
            continue
        topic["slug"] = slug
        return topic
    return None


def load_reference_article(cfg: dict, slugs: set[str]) -> tuple[str, str]:
    """Relit un article existant : il sert de gabarit (jamais de template en dur)."""
    preferred = cfg.get("reference_article_slug")
    candidates = [preferred] if preferred in slugs else []
    candidates += sorted(s for s in slugs if s != preferred)
    for slug in candidates:
        path = BLOG_DIR / slug / "index.html"
        if path.exists():
            return slug, path.read_text(encoding="utf-8")
    raise FileNotFoundError(
        "Aucun article de référence dans /blog/ : impossible de déduire le gabarit.")


# ─────────────────────────────────────────────────────────────
# Rédaction : le modèle ne produit QUE du contenu éditorial
# ─────────────────────────────────────────────────────────────

def volume_rank(errors: list[str], wc: int) -> tuple[int, int]:
    """Clé de comparaison entre deux copies : celle qui a le moins d'erreurs
    prime, puis on préfère celle qui approche le mieux la cible."""
    deficit = max(0, PROMPT_MIN_WORDS - wc)
    excess = max(0, wc - MAX_WORDS)
    return (len(errors), deficit + excess)


def build_correction(cfg: dict, errors: list[str], wc: int) -> str:
    """Message de reprise adressé au modèle. Il ne porte pas seulement sur le
    volume : toute erreur de validation que le modèle peut corriger lui-même
    (maillage interne, nombre de questions, longueur du title) y passe, tant
    qu'il reste des appels au budget."""
    demands = []
    if wc < PROMPT_MIN_WORDS:
        demands.append(
            f"Tu as généré {wc} mots pour le corps (FAQ exclue), il en faut au moins "
            f"{PROMPT_MIN_WORDS}. Développe chaque section : ajoute des paragraphes, "
            "des exemples concrets, du contexte local, des nuances. Ne retire aucune "
            "section.")
    elif wc > MAX_WORDS:
        demands.append(
            f"Tu as généré {wc} mots pour le corps (FAQ exclue), c'est trop : il en "
            f"faut au plus {PROMPT_MAX_WORDS}. Resserre chaque section sans en "
            "supprimer aucune.")

    if any("maillage" in e for e in errors):
        targets = "\n".join(f"  {t}" for t in cfg["internal_link_targets"])
        demands.append(
            "Il manque des liens internes, c'est rédhibitoire. Insère dans le corps "
            "au moins DEUX liens markdown vers ces chemins exacts, placés dans deux "
            f"sections différentes :\n{targets}\net au moins UN lien vers /blog/. "
            f"Écris-les sous la forme [libellé descriptif]({cfg['internal_link_targets'][0]}), "
            "en recopiant le chemin tel quel. Ne touche à rien d'autre.")

    others = [e for e in errors if "maillage" not in e and "volume" not in e]
    if others:
        demands.append("Corrige aussi ces points : " + " ; ".join(others) + ".")

    if not demands:
        demands.append("Reprends ton JSON en respectant toutes les consignes.")
    return " ".join(demands) + " Réponds par le seul objet JSON complet."


def build_prompt(cfg: dict, topic: dict, rules: str) -> tuple[str, str]:
    """Prompt court : plus de gabarit HTML à recopier, plus de contraintes de
    balisage. Le modèle écrit, le script fabrique la page."""
    targets = cfg["internal_link_targets"]
    targets_bullets = "\n".join(f"    {t}" for t in targets)

    system = f"""Tu es rédacteur SEO/GEO senior pour une entreprise locale française.
Tu écris du CONTENU, jamais du HTML : la mise en page est faite par ailleurs.

Tu réponds UNIQUEMENT par un objet JSON valide, sans bloc de code markdown,
respectant exactement ce schéma :

{{
  "title": "titre de la page, 55 à 60 caractères, sans le nom du site",
  "h1": "titre affiché en haut de l'article, court et percutant",
  "breadcrumb": "libellé court pour le fil d'Ariane (2 à 6 mots)",
  "meta_description": "résumé de moins de 155 caractères",
  "image_alt": "description de la photo d'en-tête, 8 à 15 mots, ancrée localement",
  "lede": "chapô d'introduction, 60 à 90 mots, qui plante une situation concrète",
  "sections": [
    {{"h2": "titre de section",
      "content": [
        {{"type": "p", "text": "paragraphe"}},
        {{"type": "h3", "text": "sous-titre"}},
        {{"type": "ul", "items": ["élément", "élément"]}},
        {{"type": "ol", "items": ["étape", "étape"]}}
      ]}}
  ],
  "faq": [{{"question": "…", "answer": "…"}}]
}}

RÈGLES DE CONTENU
- Volume : le corps (lede + sections, FAQ exclue) fait entre {PROMPT_MIN_WORDS} et
  {PROMPT_MAX_WORDS} mots. Compte les mots avant de répondre. C'est la contrainte
  la plus importante : en dessous de {PROMPT_MIN_WORDS} mots, la réponse est rejetée.
- Vise 5 à 7 sections « h2 », chacune avec 3 à 5 paragraphes nourris. Un paragraphe
  fait 60 à 110 mots : développe, donne des exemples concrets, du contexte local,
  des nuances. Ne fais jamais de paragraphe d'une seule phrase.
- FAQ : exactement {{faq_count}} questions, avec des réponses de 40 à 70 mots.
  Elles ne comptent pas dans le volume du corps.
- Balisage inline autorisé dans les textes, et lui seul :
  **gras** et [libellé](/chemin). Les liens sont forcément internes.
- Maillage interne — OBLIGATOIRE, la réponse est rejetée sans cela :
  place AU MOINS DEUX liens markdown vers ces chemins exacts, dans deux
  sections différentes du corps :
{targets_bullets}
  et AU MOINS UN lien vers /blog/.
  Forme attendue, à recopier telle quelle : [libellé descriptif]({targets[0]})
  Recopie les chemins sans les modifier, sans domaine et sans rien y ajouter.
- Ancres de liens : les libellés des liens internes doivent être descriptifs et
  se lire naturellement dans la phrase. Interdit : les libellés secs d'un seul
  mot comme « ici », « blog », « contact », « cuisine ».

GARDE-FOUS — NON NÉGOCIABLES
N'invente AUCUN prix, AUCUNE fourchette budgétaire, AUCUN chiffre d'affaires ou
de fréquentation, AUCUN nom de client, AUCUN délai chiffré en jours, AUCUNE date
de fondation, AUCUNE norme ou réglementation, AUCUN dispositif d'aide, AUCUN
label, AUCUN avis client, AUCUN horaire, AUCUNE adresse autre que ceux fournis
ci-dessous. Si une information te manque, reformule pour t'en passer.

FAITS AUTORISÉS (seule source de faits chiffrés, d'adresses et d'horaires)
{{facts}}
""".replace("{faq_count}", str(cfg["faq_questions_count"])).replace(
        "{facts}", "\n".join(f"- {f}" for f in cfg.get("facts", [])))

    user = f"""Sujet n°{topic['num']} : {topic['title']}
Angle : {topic['brief'] or "à développer librement dans le cadre des règles"}

Entreprise : {cfg['site_name']} — {cfg['sector']}.
Zone : {cfg['location']}.
Ton : {cfg['tone']}. Langue : français.

Mots-clés géographiques à faire vivre naturellement (pas de bourrage) :
{', '.join(cfg['geo_keywords'])}.

RÈGLES ÉDITORIALES DU BLOG
{rules}

Réponds par le seul objet JSON."""

    return system, user


def generate_content(cfg: dict, system: str, user: str,
                     followup: list[dict] | None = None) -> dict:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "Le paquet 'openai' n'est pas installé (pip install openai).") from exc

    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("Variable d'environnement OPENAI_API_KEY absente.")

    client = OpenAI()
    log(f"Appel OpenAI (modèle {cfg['model']}, temperature {cfg['temperature']})…")
    response = client.chat.completions.create(
        model=cfg["model"],
        temperature=cfg["temperature"],
        max_tokens=9000,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            *(followup or []),
        ],
    )
    content = (response.choices[0].message.content or "").strip()
    usage = getattr(response, "usage", None)
    if usage:
        log(f"Tokens : {usage.prompt_tokens} entrée + "
            f"{usage.completion_tokens} sortie = {usage.total_tokens}")
    if not content:
        raise ValueError("réponse vide")
    return json.loads(content)


def mock_content(cfg: dict, topic: dict) -> dict:
    """Contenu de démonstration pour --mock : même forme que la sortie du modèle,
    calibré pour dépasser la cible de volume."""
    targets = cfg["internal_link_targets"]
    filler = ("Dans les Hautes-Pyrénées, la question se pose rarement de la même "
              "manière d'un logement à l'autre. Entre une maison de ville tarbaise, "
              "un appartement à rénover et une construction récente en périphérie, "
              "les contraintes de départ diffèrent au point de changer complètement "
              "l'ordre des décisions. C'est précisément pour cette raison qu'il vaut "
              "la peine de détailler chaque cas de figure plutôt que de donner une "
              "réponse unique, qui ne conviendrait qu'à une minorité des situations "
              "rencontrées sur le terrain au fil des projets accompagnés.")
    sections = []
    for i in range(7):          # 7 sections : le mock dépasse la cible de 1200
        content = [{"type": "p", "text": filler}, {"type": "p", "text": filler}]
        if i == 0:
            content.insert(1, {"type": "h3", "text": "Un point de départ concret"})
            content.append({"type": "p",
                            "text": f"Le détail figure sur [notre page cuisine]({targets[0]}) "
                                    f"et dans [l'ensemble de nos prestations]({targets[1]})."})
        if i == 1:
            content.append({"type": "ul", "items": ["Premier repère utile",
                                                    "Deuxième repère utile",
                                                    "Troisième repère utile"]})
        if i == 2:
            content.append({"type": "p",
                            "text": "D'autres conseils sont réunis sur "
                                    "[le blog ADesign](/blog/)."})
        sections.append({"h2": f"Section de démonstration n°{i + 1}", "content": content})
    return {
        "title": f"{topic['title'][:50]} | démo",
        "h1": topic["title"],
        "breadcrumb": topic["title"][:40],
        "meta_description": f"{topic['title'][:110]} — contenu de démonstration.",
        "image_alt": "Cuisine équipée sur mesure réalisée par ADesign à Tarbes",
        "lede": filler,
        "sections": sections,
        "faq": [{"question": f"Question de démonstration n°{i + 1} ?",
                 "answer": filler[:220]} for i in range(cfg["faq_questions_count"])],
    }


# ─────────────────────────────────────────────────────────────
# Validation du contenu
# ─────────────────────────────────────────────────────────────

CONTENT_TYPES = {"p", "h3", "ul", "ol", "strong"}


def validate_content(data: dict, cfg: dict) -> list[str]:
    """Contrôles bloquants sur le CONTENU. Tout ce que le script fabrique
    lui-même (canonical, OG, JSON-LD, marqueur, fil d'Ariane, structure) ne peut
    plus être erroné et n'est donc plus contrôlé ici."""
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["la réponse n'est pas un objet JSON"]

    for key in ("title", "h1", "breadcrumb", "meta_description", "lede"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            errors.append(f"champ « {key} » absent ou vide")

    title = data.get("title", "")
    if isinstance(title, str) and not 40 <= len(title) <= 70:
        errors.append(f"title hors bornes : {len(title)} caractères (attendu 40–70)")

    desc = data.get("meta_description", "")
    if isinstance(desc, str) and len(desc) >= 155:
        errors.append(f"meta description trop longue ({len(desc)} caractères)")

    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        errors.append("aucune section")
    else:
        for i, section in enumerate(sections, 1):
            if not isinstance(section, dict) or not section.get("h2"):
                errors.append(f"section n°{i} sans titre h2")
                continue
            blocks = section.get("content")
            if not isinstance(blocks, list) or not blocks:
                errors.append(f"section n°{i} sans contenu")
                continue
            for block in blocks:
                if not isinstance(block, dict):
                    errors.append(f"section n°{i} : bloc de contenu invalide")
                    continue
                kind = block.get("type")
                if kind not in CONTENT_TYPES:
                    errors.append(f"section n°{i} : type de bloc inconnu ({kind!r})")
                elif kind in ("ul", "ol"):
                    items = block.get("items") or block.get("text")
                    if not items:
                        errors.append(f"section n°{i} : liste {kind} vide")
                elif not block.get("text"):
                    errors.append(f"section n°{i} : bloc {kind} sans texte")

    faq = data.get("faq")
    if not isinstance(faq, list) or len(faq) != cfg["faq_questions_count"]:
        errors.append(f"{cfg['faq_questions_count']} questions attendues dans la FAQ "
                      f"(trouvé : {len(faq) if isinstance(faq, list) else 0})")
    else:
        for i, item in enumerate(faq, 1):
            if not isinstance(item, dict) or not item.get("question") or not item.get("answer"):
                errors.append(f"question de FAQ n°{i} incomplète")

    # Maillage interne : toujours dépendant du modèle, donc toujours contrôlé.
    body = " ".join(
        [data.get("lede", "")] +
        [b.get("text", "") + " " + " ".join(b.get("items") or [])
         for s in (sections if isinstance(sections, list) else [])
         if isinstance(s, dict)
         for b in (s.get("content") or []) if isinstance(b, dict)])
    links = re.findall(r"\[[^\]]+\]\((/[^)\s]*)\)", body)
    targets = cfg["internal_link_targets"]
    if sum(1 for h in links if h in targets) < 2:
        errors.append("maillage interne : moins de deux liens vers "
                      + " ou ".join(targets))
    if not any(h.startswith("/blog") for h in links):
        errors.append("maillage interne : aucun lien vers /blog/")

    wc = content_word_count(data)
    if not MIN_WORDS <= wc <= MAX_WORDS:
        errors.append(f"volume hors bornes : {wc} mots (attendu {MIN_WORDS}–{MAX_WORDS})")

    return errors


# ─────────────────────────────────────────────────────────────
# Assemblage du HTML à partir du gabarit
# ─────────────────────────────────────────────────────────────

def split_template(reference_html: str) -> dict:
    """Découpe le gabarit relu en morceaux réutilisables.

    Conventions HTML propres à ADesign, différentes de celles du site de
    référence dont ce pipeline est issu :
      · `<!doctype html>` en minuscules ;
      · pas de balise <main> : le corps est un `<article class="post">`
        contenant un `<div class="container">` ;
      · les blocs JSON-LD sont précédés de commentaires encadrés
        « DONNÉES STRUCTURÉES » ; on repère donc le premier <script
        application/ld+json> puis on remonte au commentaire qui l'introduit ;
      · le fil d'Ariane vit dans son propre `<div class="container">`, hors
        de l'article ;
      · la FAQ utilise des `<div class="faq-item">` (h3 + p) ;
      · le CTA est un `<aside class="post-cta">`, suivi d'un
        `<p class="post-back">`.

    Tout ce qui n'est pas propre à un article (favicons, polices, topbar,
    header, footer, scripts, CTA, lien de retour) est repris tel quel : si le
    gabarit évolue, les articles suivants suivent.
    """
    parts: dict[str, str] = {}

    first_ld = reference_html.find('<script type="application/ld+json">')
    head_end = reference_html.find("</head>")
    if first_ld == -1 or head_end == -1:
        raise ValueError("Gabarit : bloc JSON-LD ou </head> introuvable.")
    # Le commentaire encadré qui introduit le premier JSON-LD fait partie du
    # bloc à remplacer, pas du head conservé.
    comment_start = reference_html.rfind("<!--", 0, first_ld)
    ld_start = comment_start if comment_start != -1 else first_ld
    parts["head_top"] = reference_html[:ld_start]          # du DOCTYPE au CSS

    breadcrumb_start = reference_html.find("<!-- FIL D'ARIANE -->")
    article_start = reference_html.find('<article class="post">')
    article_end = reference_html.find("</article>")
    if min(breadcrumb_start, article_start, article_end) == -1:
        raise ValueError("Gabarit : fil d'Ariane ou <article class=\"post\"> introuvable.")

    # Entre </head> et le fil d'Ariane : <body>, la topbar et le header de site.
    parts["header"] = reference_html[head_end + len("</head>"):breadcrumb_start]
    # De la fin de l'article jusqu'à </html> : footer, bouton et scripts.
    parts["footer"] = reference_html[article_end + len("</article>"):]

    body_region = reference_html[article_start:article_end]

    cta = re.search(r'<aside class="post-cta">.*?</aside>', body_region, re.S)
    parts["cta"] = cta.group().strip() if cta else ""

    back = re.search(r'<p class="post-back">.*?</p>', body_region, re.S)
    parts["back"] = back.group().strip() if back else ""

    cover = re.search(r'<img class="post-cover"[^>]*/>', body_region)
    parts["cover"] = cover.group() if cover else ""
    return parts


def build_head(parts: dict, cfg: dict, data: dict, url: str, today: dict) -> str:
    """Reprend le <head> du gabarit et n'y remplace que ce qui est propre à
    l'article. Les valeurs viennent du script, jamais du modèle en HTML."""
    head = parts["head_top"]
    title = f"{plain(data['title'])} | {cfg['site_name']}"
    desc = plain(data["meta_description"])
    img = f"{cfg['site_url']}{cfg['og_image']}"
    alt = plain(data.get("image_alt") or
                f"{cfg['site_name']} — {cfg['sector']} à {cfg['location']}")

    def swap(pattern: str, replacement: str, text: str, required: bool = True) -> str:
        new, n = re.subn(pattern, lambda _: replacement, text, count=1)
        if n != 1 and required:
            raise ValueError(f"Gabarit : motif introuvable dans le <head> — {pattern}")
        return new

    # La meta description d'ADesign est écrite sur deux lignes : le motif doit
    # tolérer le retour à la ligne entre l'attribut name et l'attribut content.
    head = swap(r"<title>.*?</title>", f"<title>{esc(title)}</title>", head)
    head = swap(r'<meta name="description"\s+content="[^"]*"\s*/>',
                f'<meta name="description"\n        content="{esc(desc)}" />', head)
    head = swap(r'<link rel="canonical" href="[^"]*" />',
                f'<link rel="canonical" href="{url}" />', head)
    head = swap(r'<meta name="author" content="[^"]*" />',
                f'<meta name="author" content="{esc(cfg["author"])}" />', head, False)
    head = swap(r'<meta name="article:published_time" content="[^"]*" />',
                f'<meta name="article:published_time" content="{today["iso"]}" />',
                head, False)
    head = swap(r'<meta property="og:title" content="[^"]*" />',
                f'<meta property="og:title" content="{esc(plain(data["title"]))}" />', head)
    head = swap(r'<meta property="og:description" content="[^"]*" />',
                f'<meta property="og:description" content="{esc(desc)}" />', head)
    head = swap(r'<meta property="og:url" content="[^"]*" />',
                f'<meta property="og:url" content="{url}" />', head)
    head = swap(r'<meta property="og:image" content="[^"]*" />',
                f'<meta property="og:image" content="{img}" />', head)
    head = swap(r'<meta property="og:image:alt" content="[^"]*" />',
                f'<meta property="og:image:alt" content="{esc(alt)}" />', head, False)
    head = swap(r'<meta property="article:published_time" content="[^"]*" />',
                f'<meta property="article:published_time" content="{today["iso"]}" />',
                head, False)
    head = swap(r'<meta property="article:modified_time" content="[^"]*" />',
                f'<meta property="article:modified_time" content="{today["iso"]}" />',
                head, False)
    head = swap(r'<meta property="article:section" content="[^"]*" />',
                f'<meta property="article:section" content="{esc(cfg["default_article_section"])}" />',
                head, False)
    head = swap(r'<meta name="twitter:title" content="[^"]*" />',
                f'<meta name="twitter:title" content="{esc(plain(data["title"]))}" />', head)
    head = swap(r'<meta name="twitter:description" content="[^"]*" />',
                f'<meta name="twitter:description" content="{esc(desc)}" />', head)
    head = swap(r'<meta name="twitter:image" content="[^"]*" />',
                f'<meta name="twitter:image" content="{img}" />', head)
    head = swap(r'<meta name="twitter:image:alt" content="[^"]*" />',
                f'<meta name="twitter:image:alt" content="{esc(alt)}" />', head, False)
    return head


def box(title: str) -> str:
    """Commentaire encadré, au format employé partout sur le site ADesign."""
    rule = "═" * 59
    return (f"  <!-- {rule}\n"
            f"       DONNÉES STRUCTURÉES — {title}\n"
            f"       {rule} -->\n")


def build_jsonld(cfg: dict, data: dict, url: str, today: dict) -> str:
    """Les trois blocs JSON-LD, sérialisés par json.dumps : ils sont valides
    par construction, ce que le modèle ne pouvait pas garantir."""
    img = f"{cfg['site_url']}{cfg['og_image']}"
    article = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": plain(data["h1"]),
        "description": plain(data["meta_description"]),
        "image": [img],
        "datePublished": today["iso"],
        "dateModified": today["iso"],
        "inLanguage": "fr-FR",
        "author": {"@type": "Organization", "name": cfg["author"],
                   "url": f"{cfg['site_url']}/"},
        "publisher": {
            "@type": "Organization", "name": cfg["site_name"],
            "url": f"{cfg['site_url']}/",
            "logo": {"@type": "ImageObject",
                     "url": f"{cfg['site_url']}{cfg['logo_path']}"}},
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "isPartOf": {"@type": "Blog", "@id": f"{cfg['site_url']}/blog/#blog",
                     "name": f"Blog {cfg['site_name']}"},
        "about": [{"@type": "Thing", "name": cfg["default_article_section"]},
                  {"@type": "Thing", "name": "Agencement d'intérieur"}],
        "spatialCoverage": {"@type": "AdministrativeArea",
                            "name": cfg["location"].split(",")[-1].strip()},
        "articleSection": cfg["default_article_section"],
        "keywords": ", ".join(cfg["geo_keywords"][:6]),
    }
    breadcrumb = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Accueil",
             "item": f"{cfg['site_url']}/"},
            {"@type": "ListItem", "position": 2, "name": "Blog",
             "item": f"{cfg['site_url']}/blog/"},
            {"@type": "ListItem", "position": 3, "name": plain(data["title"]),
             "item": url},
        ],
    }
    faqpage = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": plain(q["question"]),
             "acceptedAnswer": {"@type": "Answer", "text": plain(q["answer"])}}
            for q in data["faq"]
        ],
    }
    out = []
    for label, payload in (("Article", article), ("BreadcrumbList", breadcrumb),
                           ("FAQPage", faqpage)):
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        body = "\n".join("  " + line for line in body.splitlines())
        out.append(f'{box(label)}  <script type="application/ld+json">\n'
                   f'{body}\n  </script>\n')
    return "\n".join(out)


def render_blocks(blocks: list[dict]) -> str:
    """Contenu d'une section, converti en HTML. Le modèle n'écrit que du texte :
    c'est ici, et seulement ici, que le balisage apparaît."""
    out = []
    for block in blocks:
        kind = block.get("type")
        if kind in ("ul", "ol"):
            items = block.get("items")
            if not items:
                items = [s for s in re.split(r"\s*[;\n]\s*", block.get("text", "")) if s]
            lines = "\n".join(f"          <li>{inline(i)}</li>" for i in items)
            out.append(f"        <{kind}>\n{lines}\n        </{kind}>")
        elif kind == "h3":
            out.append(f"        <h3>{inline(block['text'])}</h3>")
        elif kind == "strong":
            out.append(f"        <p><strong>{inline(block['text'])}</strong></p>")
        else:
            out.append(f"        <p>{inline(block['text'])}</p>")
    return "\n\n".join(out)


def build_main(parts: dict, cfg: dict, data: dict, today: dict) -> str:
    """Fil d'Ariane + article complet, aux conventions HTML d'ADesign.
    Le CTA et le lien de retour sont repris du gabarit."""
    reading = max(3, round(content_word_count(data) / 200))
    img = cfg["og_image"]
    alt = plain(data.get("image_alt") or
                f"{cfg['site_name']} — {cfg['sector']} à {cfg['location']}")

    body = "\n\n".join(
        f"        <h2>{inline(s['h2'])}</h2>\n\n{render_blocks(s['content'])}"
        for s in data["sections"])

    faq = "\n\n".join(
        f'        <div class="faq-item">\n'
        f'          <h3>{inline(q["question"])}</h3>\n'
        f'          <p>{inline(q["answer"])}</p>\n'
        f'        </div>'
        for q in data["faq"])

    cta = f"\n\n      <!-- CTA -->\n      {parts['cta']}" if parts["cta"] else ""
    back = f"\n\n      {parts['back']}" if parts["back"] else ""

    return f"""  <!-- FIL D'ARIANE -->
  <div class="container">
    <nav class="breadcrumb" aria-label="Fil d'Ariane">
      <ol>
        <li><a href="/index.html">Accueil</a></li>
        <li><a href="/blog/">Blog</a></li>
        <li><span aria-current="page">{inline(data['breadcrumb'])}</span></li>
      </ol>
    </nav>
  </div>

  <!-- ARTICLE -->
  <article class="post">
    <div class="container">
      <header class="post-header">
        <h1>{inline(data['h1'])}</h1>
        <p class="post-meta">
          <time datetime="{today['iso']}">{today['fr']}</time>
          <span class="sep">·</span>
          <span>Par {esc(cfg['author'])}</span>
          <span class="sep">·</span>
          <span>Lecture : {reading} min</span>
        </p>
      </header>

      <img class="post-cover" src="{img}" alt="{esc(alt)}" width="1200" height="700" />

      <div class="post-body">

        <p>{inline(data['lede'])}</p>

{body}

      </div>

      <!-- FAQ -->
      <section class="faq" aria-labelledby="faq-title">
        <h2 id="faq-title">Questions fréquentes</h2>

{faq}
      </section>{cta}{back}

    </div>
  </article>"""


def assemble(reference_html: str, cfg: dict, topic: dict,
             data: dict, today: dict) -> str:
    """Fabrique la page complète. Toute la structure vient d'ici : le modèle
    n'a produit que du texte."""
    parts = split_template(reference_html)
    url = f"{cfg['site_url']}/blog/{topic['slug']}/"
    marker = f"<!-- {cfg['topic_marker_prefix']}: {topic['num']} -->"

    head = build_head(parts, cfg, data, url, today)
    jsonld = build_jsonld(cfg, data, url, today)
    header = parts["header"].replace("<body>", f"<body>\n{marker}", 1)

    return (head + jsonld + "</head>" + header
            + build_main(parts, cfg, data, today) + parts["footer"])


def validate_assembled(html: str, cfg: dict, topic: dict) -> list[str]:
    """Filet de sécurité sur l'assemblage : ces contrôles ne portent plus sur le
    modèle mais sur notre propre code. Ils doivent toujours passer."""
    errors = []
    url = f"{cfg['site_url']}/blog/{topic['slug']}/"
    if not html.lstrip().lower().startswith("<!doctype html>"):
        errors.append("assemblage : DOCTYPE absent")
    if not html.rstrip().endswith("</html>"):
        errors.append("assemblage : </html> absent")
    if f"{cfg['topic_marker_prefix']}: {topic['num']}" not in html:
        errors.append("assemblage : marqueur d'idempotence absent")
    if html.count("<h1") != 1:
        errors.append(f"assemblage : {html.count('<h1')} balise(s) h1")
    if f'rel="canonical" href="{url}"' not in html:
        errors.append("assemblage : canonical incorrect")
    if html.count("<article class=\"post\">") != 1:
        errors.append("assemblage : article principal absent ou dupliqué")
    if html.count("</article>") != 1:
        errors.append("assemblage : balise </article> absente ou dupliquée")
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    if len(blocks) != 3:
        errors.append(f"assemblage : {len(blocks)} blocs JSON-LD au lieu de 3")
    for i, block in enumerate(blocks, 1):
        try:
            json.loads(block)
        except json.JSONDecodeError as exc:
            errors.append(f"assemblage : JSON-LD n°{i} invalide ({exc})")
    if html.count('class="faq-item"') != cfg["faq_questions_count"]:
        errors.append("assemblage : nombre de questions de FAQ incorrect")
    return errors


def extract(data: dict) -> dict:
    """Métadonnées utilisées par blog/index.html, blog/rss.xml et llms.txt."""
    return {
        "title": plain(data["title"]),
        "description": plain(data["meta_description"]),
        "h1": plain(data["h1"]),
        "headline": plain(data["h1"]),
        "lead": plain(data["lede"]),
        "image_alt": plain(data.get("image_alt", "")),
        "words": content_word_count(data),
    }


# ─────────────────────────────────────────────────────────────
# Mises à jour des fichiers annexes
# ─────────────────────────────────────────────────────────────

def update_blog_index(cfg: dict, topic: dict, meta: dict, today: dict) -> str:
    html = BLOG_INDEX.read_text(encoding="utf-8")
    url = f"/blog/{topic['slug']}/"
    if url in html:
        log("blog/index.html contient déjà cet article : pas de doublon ajouté.")
        return html

    headline = meta["headline"] or meta["h1"] or topic["title"]
    teaser = meta["lead"] or meta["description"]
    if len(teaser) > 320:
        teaser = teaser[:317].rsplit(" ", 1)[0] + "…"
    alt = meta["image_alt"] or f"{cfg['site_name']} — {cfg['sector']}"

    card = f"""

        <!-- ══ ARTICLE ══ -->
        <article class="post-card">
          <a href="{url}" aria-label="Lire : {esc(headline)}">
            <img class="thumb" src="{cfg['og_image']}" alt="{esc(alt)}" width="800" height="600" loading="lazy" />
          </a>
          <div class="content">
            <p class="meta"><time datetime="{today['iso']}">{today['fr']}</time></p>
            <h2><a href="{url}">{esc(headline)}</a></h2>
            <p>{esc(teaser)}</p>
            <a class="btn" href="{url}">LIRE L’ARTICLE</a>
          </div>
        </article>
        <!-- ══ FIN ARTICLE ══ -->
"""
    anchor = '<div class="post-grid">'
    if anchor not in html:
        raise ValueError("Point d'insertion .post-grid introuvable dans blog/index.html")
    html = html.replace(anchor, anchor + card, 1)

    entry = f"""
      {{
        "@type": "BlogPosting",
        "headline": "{headline.replace('"', "'")}",
        "url": "{cfg['site_url']}{url}",
        "datePublished": "{today['iso']}",
        "author": {{ "@type": "Organization", "name": "{cfg['author']}" }}
      }},"""
    ld_anchor = '"blogPost": ['
    if ld_anchor in html:
        html = html.replace(ld_anchor, ld_anchor + entry, 1)
    else:
        log("Avertissement : tableau blogPost introuvable, JSON-LD de l'index inchangé.")
    return html


def update_sitemap(cfg: dict, topic: dict, today: dict) -> str:
    xml = SITEMAP.read_text(encoding="utf-8")
    loc = f"{cfg['site_url']}/blog/{topic['slug']}/"
    if loc in xml:
        log("sitemap.xml contient déjà cette URL.")
        return xml

    xml = re.sub(
        rf"(<loc>{re.escape(cfg['site_url'])}/blog/</loc>\s*<lastmod>)[^<]*(</lastmod>)",
        rf"\g<1>{today['iso']}\g<2>", xml)

    entry = f"""  <url>
    <loc>{loc}</loc>
    <lastmod>{today['iso']}</lastmod>
    <changefreq>yearly</changefreq>
    <priority>0.8</priority>
  </url>

</urlset>"""
    return xml.replace("</urlset>", entry, 1)


def update_rss(cfg: dict, topic: dict, meta: dict, today: dict) -> str:
    xml = RSS.read_text(encoding="utf-8")
    link = f"{cfg['site_url']}/blog/{topic['slug']}/"
    if link in xml:
        log("blog/rss.xml contient déjà cet article.")
        return xml

    def xesc(text: str) -> str:
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    headline = meta["headline"] or meta["h1"] or topic["title"]
    teaser = meta["lead"] or meta["description"]
    pub = rfc822(today["date"])

    xml = re.sub(r"<lastBuildDate>[^<]*</lastBuildDate>",
                 f"<lastBuildDate>{pub}</lastBuildDate>", xml, count=1)

    item = f"""    <item>
      <title>{xesc(headline)}</title>
      <link>{link}</link>
      <guid isPermaLink="true">{link}</guid>
      <pubDate>{pub}</pubDate>
      <author>adesign-france@adesign-france.fr ({cfg['author']})</author>
      <category>{xesc(cfg['default_article_section'])}</category>
      <description>{xesc(teaser)}</description>
    </item>

"""
    if "<item>" in xml:
        idx = xml.index("    <item>")
        return xml[:idx] + item + xml[idx:]
    return xml.replace("  </channel>", item + "  </channel>", 1)


def update_llms(cfg: dict, topic: dict, meta: dict) -> str | None:
    if not LLMS.exists():
        return None
    text = LLMS.read_text(encoding="utf-8")
    url = f"{cfg['site_url']}/blog/{topic['slug']}/"
    if url in text:
        log("llms.txt référence déjà cet article.")
        return text
    headline = meta["headline"] or meta["h1"] or topic["title"]
    summary = (meta["description"] or "").rstrip(".")
    line = f"- [{headline}]({url}) : {summary}.\n"
    m = re.search(r"^## Blog\s*$(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
    if not m:
        log("Avertissement : section « ## Blog » introuvable dans llms.txt.")
        return text
    block = m.group(1).rstrip("\n")
    return text[:m.start(1)] + block + "\n" + line + "\n" + text[m.end(1):]


# ─────────────────────────────────────────────────────────────
# Point d'entrée
# ─────────────────────────────────────────────────────────────

def refresh_entries(cfg: dict, topic: dict, meta: dict) -> list[str]:
    """Après réécriture d'un article existant, resynchronise le teaser de
    blog/index.html et l'entrée RSS : les updaters sont idempotents par URL et
    laisseraient sinon en place le texte de l'ancienne version."""
    touched = []
    slug = topic["slug"]
    teaser = meta["lead"] or meta["description"]
    if len(teaser) > 320:
        teaser = teaser[:317].rsplit(" ", 1)[0] + "…"

    html = BLOG_INDEX.read_text(encoding="utf-8")
    card = re.search(r'<article class="post-card">(?:(?!</article>).)*?/blog/'
                     + re.escape(slug) + r'/(?:(?!</article>).)*?</article>', html, re.S)
    if card:
        new_card = re.sub(r"<p>(?!<)[^<]*</p>",
                          f"<p>{esc(teaser)}</p>", card.group(), count=1)
        new_card = re.sub(r'(<h2><a href="/blog/' + re.escape(slug) + r'/">)[^<]*',
                          lambda m: m.group(1) + esc(meta["headline"]), new_card, count=1)
        if new_card != card.group():
            BLOG_INDEX.write_text(html.replace(card.group(), new_card, 1), encoding="utf-8")
            touched.append("blog/index.html")

    xml = RSS.read_text(encoding="utf-8")
    item = re.search(r"<item>(?:(?!</item>).)*?" + re.escape(slug)
                     + r"(?:(?!</item>).)*?</item>", xml, re.S)
    if item:
        new_item = re.sub(r"<description>.*?</description>",
                          f"<description>{esc(teaser)}</description>",
                          item.group(), count=1, flags=re.S)
        new_item = re.sub(r"<title>.*?</title>",
                          f"<title>{esc(meta['headline'])}</title>",
                          new_item, count=1, flags=re.S)
        if new_item != item.group():
            RSS.write_text(xml.replace(item.group(), new_item, 1), encoding="utf-8")
            touched.append("blog/rss.xml")
    return touched


def main() -> int:
    parser = argparse.ArgumentParser(description="Génère un article de blog ADesign.")
    parser.add_argument("--dry-run", action="store_true",
                        help="n'écrit aucun fichier, affiche le résultat")
    parser.add_argument("--mock", action="store_true",
                        help="n'appelle pas l'API OpenAI (contenu de démonstration)")
    parser.add_argument("--rewrite", metavar="SLUG",
                        help="réécrit un article existant et écrase son fichier")
    args = parser.parse_args()

    if args.dry_run:
        log("Mode DRY-RUN : aucun fichier ne sera écrit.")

    try:
        cfg = load_config()
        log(f"Site : {cfg['site_name']} — {cfg['site_url']}")

        if not WORKFLOW_PATH.exists():
            fail(f"BLOG_WORKFLOW.md introuvable ({WORKFLOW_PATH}).")
            return EXIT_ERROR
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

        topics = parse_topics(workflow)
        rules = parse_editorial_rules(workflow)
        log(f"{len(topics)} sujets listés dans BLOG_WORKFLOW.md.")
        if not rules:
            log("Avertissement : règles éditoriales non trouvées, prompt allégé.")

        done, slugs = scan_blog(cfg["topic_marker_prefix"])
        log(f"Articles déjà en ligne : {len(slugs)} — sujets marqués traités : "
            f"{sorted(done) if done else 'aucun'}")

        if args.rewrite:
            # Réécriture : on retrouve le sujet par le marqueur du fichier existant.
            target_file = BLOG_DIR / args.rewrite / "index.html"
            if not target_file.exists():
                fail(f"Article introuvable : {target_file.relative_to(ROOT)}")
                return EXIT_ERROR
            existing = target_file.read_text(encoding="utf-8")
            m = re.search(rf"<!--\s*{re.escape(cfg['topic_marker_prefix'])}:\s*(\d+)\s*-->",
                          existing)
            if not m:
                fail(f"Aucun marqueur de sujet dans {target_file.relative_to(ROOT)} : "
                     "impossible de savoir quel sujet réécrire.")
                return EXIT_ERROR
            num = int(m.group(1))
            topic = next((t for t in topics if t["num"] == num), None)
            if topic is None:
                fail(f"Le sujet n°{num} n'existe plus dans BLOG_WORKFLOW.md.")
                return EXIT_ERROR
            topic["slug"] = args.rewrite
            log(f"Mode RÉÉCRITURE : sujet n°{num} — {topic['title']}")
        else:
            topic = pick_topic(topics, done, slugs)
            if topic is None:
                log("Aucun sujet restant à traiter. Ajoutez des sujets dans "
                    "BLOG_WORKFLOW.md (section « Sujets suggérés »).")
                return EXIT_NOTHING_TODO
            log(f"Sujet retenu : n°{topic['num']} — {topic['title']}")
            target_file = BLOG_DIR / topic["slug"] / "index.html"
            if target_file.exists():
                fail(f"Le fichier existe déjà : {target_file.relative_to(ROOT)} — "
                     "rien n'est écrasé (--rewrite pour le régénérer).")
                return EXIT_NOTHING_TODO

        log(f"Slug : {topic['slug']}")

        ref_slug, reference_html = load_reference_article(cfg, slugs)
        log(f"Gabarit relu depuis /blog/{ref_slug}/index.html "
            f"({len(reference_html)} caractères).")

        today_date = dt.date.today()
        today = {"date": today_date, "iso": today_date.isoformat(),
                 "fr": fr_date(today_date)}

        system = user = None
        if args.mock:
            log("Mode MOCK : contenu de démonstration, aucun appel API.")
            data = mock_content(cfg, topic)
        else:
            system, user = build_prompt(cfg, topic, rules)
            log(f"Prompt construit ({len(system)} car. système + "
                f"{len(user)} car. utilisateur).")
            data = generate_content(cfg, system, user)

        errors = validate_content(data, cfg)
        wc = content_word_count(data)

        # Rattrapage : on relance tant qu'il reste une erreur que le modèle peut
        # corriger — volume hors cible, maillage absent, etc. —, dans la limite
        # de MAX_CALLS appels au total. Chaque reprise repart de la MEILLEURE
        # copie obtenue jusque-là, pas de la dernière : le modèle développe
        # alors un texte déjà long au lieu de repartir d'un plus court.
        calls = 1
        while (not args.mock and calls < MAX_CALLS
               and (errors or not PROMPT_MIN_WORDS <= wc <= MAX_WORDS)):
            correction = build_correction(cfg, errors, wc)
            calls += 1
            reason = (f"{wc} mots, cible {PROMPT_MIN_WORDS}"
                      if not PROMPT_MIN_WORDS <= wc <= MAX_WORDS
                      else f"{len(errors)} erreur(s) de validation")
            log(f"Copie à reprendre ({reason}) — tentative {calls}/{MAX_CALLS}.")
            try:
                retry = generate_content(cfg, system, user, followup=[
                    {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
                    {"role": "user", "content": correction},
                ])
            except (ValueError, json.JSONDecodeError) as exc:
                fail(f"Tentative {calls} inexploitable : {exc}")
                break
            retry_errors = validate_content(retry, cfg)
            retry_wc = content_word_count(retry)
            log(f"Tentative {calls} : {retry_wc} mots, {len(retry_errors)} erreur(s).")
            if volume_rank(retry_errors, retry_wc) < volume_rank(errors, wc):
                data, errors, wc = retry, retry_errors, retry_wc
                log(f"Copie retenue : la n°{calls}.")
            else:
                log("Copie retenue : la précédente (la nouvelle n'est pas meilleure).")
        if calls > 1:
            log(f"{calls} appels OpenAI au total pour cet article.")

        if errors:
            fail("Contenu rejeté par la validation — aucun fichier écrit :")
            for err in errors:
                fail(f"  · {err}")
            return EXIT_ERROR

        html = assemble(reference_html, cfg, topic, data, today)
        build_errors = validate_assembled(html, cfg, topic)
        if build_errors:
            fail("Assemblage HTML incorrect — aucun fichier écrit :")
            for err in build_errors:
                fail(f"  · {err}")
            return EXIT_ERROR

        meta = extract(data)
        log("Validation OK.")
        log(f"  Titre       : {meta['title']}")
        log(f"  Description : {meta['description']} ({len(meta['description'])} car.)")
        log(f"  Volume      : {meta['words']} mots (corps hors FAQ)")
        log(f"  Page        : {len(html)} caractères, "
            f"{len(data['sections'])} sections")

        if args.dry_run:
            print("\n" + "═" * 70)
            print("APERÇU (aucun fichier écrit)")
            print("═" * 70)
            print(f"Sujet       : n°{topic['num']} — {topic['title']}")
            print(f"Slug        : {topic['slug']}")
            print(f"URL         : {cfg['site_url']}/blog/{topic['slug']}/")
            print(f"Titre       : {meta['title']}")
            print(f"H1          : {meta['h1']}")
            print(f"Description : {meta['description']}")
            print(f"Mots        : {meta['words']}")
            print("-" * 70)
            for section in data["sections"]:
                print(f"  H2 · {plain(section['h2'])}")
            print("═" * 70)
            log("DRY-RUN terminé, rien n'a été modifié.")
            return EXIT_OK

        # ── Écriture (au plus tard possible, une fois tout validé) ──
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(html, encoding="utf-8")
        log(f"Écrit : {target_file.relative_to(ROOT)}")

        if args.rewrite:
            for name in refresh_entries(cfg, topic, meta):
                log(f"Resynchronisé : {name}")
            log(f"Terminé — article n°{topic['num']} réécrit : "
                f"{cfg['site_url']}/blog/{topic['slug']}/")
            return EXIT_OK

        blog_index_html = update_blog_index(cfg, topic, meta, today)
        sitemap_xml = update_sitemap(cfg, topic, today)
        rss_xml = update_rss(cfg, topic, meta, today)
        llms_txt = update_llms(cfg, topic, meta)

        BLOG_INDEX.write_text(blog_index_html, encoding="utf-8")
        log("Mis à jour : blog/index.html")
        SITEMAP.write_text(sitemap_xml, encoding="utf-8")
        log("Mis à jour : sitemap.xml")
        RSS.write_text(rss_xml, encoding="utf-8")
        log("Mis à jour : blog/rss.xml")
        if llms_txt is not None:
            LLMS.write_text(llms_txt, encoding="utf-8")
            log("Mis à jour : llms.txt")

        log(f"Terminé — article n°{topic['num']} publié : "
            f"{cfg['site_url']}/blog/{topic['slug']}/")
        return EXIT_OK

    except Exception as exc:                      # noqa: BLE001
        fail(f"{type(exc).__name__} : {exc}")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

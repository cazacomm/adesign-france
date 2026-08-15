#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Génération automatique d'un article de blog ADesign.

Le site est du HTML statique écrit à la main : il n'y a ni générateur, ni build.
Ce script reproduit donc, à l'identique, ce qu'un rédacteur ferait à la main en
suivant BLOG_WORKFLOW.md :

  1. lit blog-config.json ;
  2. extrait la liste des 12 sujets suggérés de BLOG_WORKFLOW.md (§ 7) ;
  3. scanne /blog/<slug>/index.html pour savoir quels sujets sont déjà traités ;
  4. demande à OpenAI le contenu du prochain sujet non traité ;
  5. fabrique /blog/<slug>/index.html en repartant du GABARIT (l'article de
     référence est relu à chaque exécution : header, footer, CSS et scripts
     restent donc toujours alignés sur le site) ;
  6. ajoute la card dans /blog/index.html, la ligne dans sitemap.xml et l'item
     en tête de blog/rss.xml.

Rien n'est écrit sur le disque avant que tout ait été généré et validé, et
aucun fichier existant n'est jamais réécrit : si le dossier de l'article existe
déjà, le script sort proprement.

Codes de sortie :
    0   article généré (ou dry-run réussi)
    1   erreur (API, validation, gabarit illisible…)
    78  rien à faire — tous les sujets sont traités, ou l'article existe déjà
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

ROOT = Path(__file__).resolve().parent.parent

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOTHING_TO_DO = 78

MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"]
JOURS_RFC822 = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MOIS_RFC822 = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Marqueur écrit dans chaque article généré : c'est lui qui rend le script
# idempotent (on sait quel sujet de la liste a déjà été traité, même si le
# slug retenu par le modèle diffère du libellé du sujet).
TOPIC_MARKER = "<!-- adesign-topic: {n} -->"
TOPIC_MARKER_RE = re.compile(r"<!--\s*adesign-topic:\s*(\d+)\s*-->")


# ─────────────────────────────────────────────────────────────────────────────
# Logs
# ─────────────────────────────────────────────────────────────────────────────

def log(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    log(f"→ {msg}")


def ok(msg: str) -> None:
    log(f"  ✓ {msg}")


def warn(msg: str) -> None:
    log(f"  ! {msg}")


class Fatal(Exception):
    """Erreur bloquante : message clair + exit 1."""


class NothingToDo(Exception):
    """Rien à générer : message clair + exit 78 (neutral)."""


# ─────────────────────────────────────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """« Îlot ou péninsule ? » → « ilot-ou-peninsule »."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("’", "'").replace("'", " ").replace("&", " et ")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return re.sub(r"-{2,}", "-", text)


def typo(text: str) -> str:
    """Apostrophe typographique, comme partout ailleurs sur le site."""
    return text.replace("'", "’")


def attr(text: str) -> str:
    """Texte sûr dans un attribut HTML."""
    return html.escape(text, quote=True)


def date_fr(d: datetime) -> str:
    return f"{d.day} {MOIS_FR[d.month - 1]} {d.year}"


def date_rfc822(d: datetime) -> str:
    return (f"{JOURS_RFC822[d.weekday()]}, {d.day:02d} {MOIS_RFC822[d.month - 1]} "
            f"{d.year} 09:00:00 +0200")


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise Fatal(f"lecture impossible de {path.relative_to(ROOT)} : {exc}") from exc


def sub_once(pattern: str, replacement: str, text: str, what: str,
             flags: int = 0) -> str:
    """Remplace une occurrence, ou échoue avec un message explicite.

    Le gabarit est relu à chaque exécution : si quelqu'un modifie la structure
    de l'article de référence, on préfère un échec net à un fichier mal formé.
    """
    new, count = re.subn(pattern, lambda _m: replacement, text, count=1, flags=flags)
    if count != 1:
        raise Fatal(
            f"gabarit inattendu : impossible de localiser {what}. "
            f"L'article de référence a-t-il été modifié ? (voir blog-config.json → reference_article)")
    return new


# ─────────────────────────────────────────────────────────────────────────────
# Configuration & sujets
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    path = ROOT / "blog-config.json"
    if not path.exists():
        raise Fatal("blog-config.json est introuvable à la racine du dépôt.")
    try:
        cfg = json.loads(read(path))
    except json.JSONDecodeError as exc:
        raise Fatal(f"blog-config.json est invalide : {exc}") from exc

    for key in ("site_name", "site_url", "author", "faq_questions_count"):
        if not cfg.get(key):
            raise Fatal(f"blog-config.json : la clé « {key} » est manquante.")
    cfg["site_url"] = cfg["site_url"].rstrip("/")
    return cfg


@dataclass
class Topic:
    number: int
    subject: str
    angle: str


def load_topics(cfg: dict) -> list[Topic]:
    """Extrait le tableau markdown de « § 7. Sujets suggérés » de BLOG_WORKFLOW.md."""
    doc = ROOT / cfg.get("workflow_doc", "BLOG_WORKFLOW.md")
    if not doc.exists():
        raise Fatal(f"{doc.name} est introuvable.")

    text = read(doc)
    match = re.search(r"^##\s*\d*\.?\s*Sujets suggérés.*?$(.*?)(?=^##\s|\Z)",
                      text, re.S | re.M)
    if not match:
        raise Fatal("section « Sujets suggérés » introuvable dans BLOG_WORKFLOW.md.")

    topics: list[Topic] = []
    for line in match.group(1).splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not cells[0].isdigit():
            continue                      # en-tête, séparateur, texte libre
        topics.append(Topic(int(cells[0]), typo(cells[1]), typo(cells[2])))

    if not topics:
        raise Fatal("aucun sujet lisible dans le tableau de BLOG_WORKFLOW.md § 7.")
    return topics


def scan_existing() -> tuple[set[int], set[str]]:
    """Renvoie (numéros de sujets déjà traités, slugs existants)."""
    done_topics: set[int] = set()
    slugs: set[str] = set()

    blog_dir = ROOT / "blog"
    if not blog_dir.is_dir():
        raise Fatal("le dossier /blog est introuvable.")

    for article in sorted(blog_dir.glob("*/index.html")):
        slug = article.parent.name
        slugs.add(slug)
        marker = TOPIC_MARKER_RE.search(read(article))
        if marker:
            done_topics.add(int(marker.group(1)))

    return done_topics, slugs


def pick_topic(topics: list[Topic], done: set[int], forced: int | None) -> Topic:
    if forced is not None:
        for topic in topics:
            if topic.number == forced:
                if topic.number in done:
                    raise NothingToDo(f"le sujet #{forced} a déjà été publié.")
                return topic
        raise Fatal(f"le sujet #{forced} n'existe pas dans BLOG_WORKFLOW.md § 7.")

    for topic in topics:                 # ordre séquentiel de la liste
        if topic.number not in done:
            return topic

    raise NothingToDo(
        f"les {len(topics)} sujets de BLOG_WORKFLOW.md § 7 ont tous été publiés. "
        "Ajoutez de nouvelles lignes au tableau pour relancer la machine.")


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Tu es rédacteur SEO senior pour {site_name}, {sector} basé à {location}.
Tu écris en {language}, sur un ton {tone}.

RÈGLES ÉDITORIALES ABSOLUES (les enfreindre invalide l'article) :
- Interdit : tout prix, fourchette de prix, pourcentage de remise ou de TVA.
- Interdit : tout chiffre présenté comme un fait vérifié (délais en jours, durées
  de garantie, statistiques de marché, parts de marché, dates de fondation,
  effectifs, nombre de réalisations).
- Interdit : citer une norme, une réglementation, une aide publique ou un
  dispositif fiscal (NF, DTU, MaPrimeRénov', crédit d'impôt…).
- Interdit : noms de clients, témoignages, adresses de chantiers, noms de marques
  ou de concurrents, comparaisons avec des enseignes nommées.
- Interdit : inventer quoi que ce soit sur l'entreprise (ancienneté, taille,
  certifications, récompenses).
- Autorisé et recommandé : le conseil de méthode, l'explication du « pourquoi »,
  les ordres de grandeur qualitatifs (« plusieurs semaines », « quelques
  centimètres »), l'ancrage géographique local.
- Ancrage local à répartir naturellement dans le texte : {geo_keywords}.
- Ton expert-conseil : on explique une méthode, on ne vend pas.
- Apostrophes typographiques ’ (jamais ').

FORMAT HTML du champ body_html :
- Uniquement ces balises : <h2>, <h3>, <p>, <ul>, <ol>, <li>, <strong>, <em>,
  <a href="…">, et <div class="callout"><p>…</p></div> pour 1 à 2 encadrés.
- Aucun <h1> (il est déjà dans la page), aucun <script>, <style>, <img>,
  <table>, aucun attribut de style.
- Un <h2> tous les 200 à 300 mots, structure logique du général au particulier.
- 2 à 4 liens internes en chemin ABSOLU, choisis parmi :
  /cuisine.html, /prestation-service.html, /contact.html, /presentation.html,
  /boutique.html, et l'article existant
  /blog/comment-choisir-sa-cuisine-equipee-a-tarbes-en-2026/
- Ne mets NI la FAQ NI le bloc CTA dans body_html : ils ont leurs propres champs.
"""

USER_PROMPT = """Rédige l'article de blog n°{number} de la ligne éditoriale.

Sujet : {subject}
Angle imposé : {angle}

Contraintes de longueur : environ {word_count} mots dans body_html
(1 200 à 1 500 mots), et exactement {faq_count} questions de FAQ.

Réponds UNIQUEMENT par un objet JSON valide avec exactement ces clés :

{{
  "title": "titre de l'article, 50 à 65 caractères, SANS le suffixe « | ADesign », avec un ancrage local si c'est naturel",
  "slug": "slug-en-minuscules-sans-accent-mots-cles-en-tete",
  "meta_description": "155 caractères MAXIMUM, une phrase qui donne envie et contient l'ancrage local",
  "card_excerpt": "1 à 2 phrases (200 caractères max) pour la card de la liste d'articles",
  "rss_description": "1 à 2 phrases (250 caractères max) pour le flux RSS",
  "category": "un seul mot parmi : Cuisine, Agencement, Salle de bains, Dressing, Éclairage",
  "body_html": "le corps de l'article en HTML, voir les règles de format",
  "faq": [
    {{"question": "une vraie question de client, phrase complète", "answer": "réponse de 3 à 5 phrases, en texte brut SANS balise HTML"}}
  ],
  "cta_title": "titre court du bloc d'appel à l'action, en lien avec le sujet, se terminant par un ?",
  "cta_text": "1 à 2 phrases invitant à un échange et à un relevé de cotes, sans promesse chiffrée"
}}

Rappel : meta_description fait 155 caractères maximum, c'est une contrainte
technique stricte. La FAQ contient exactement {faq_count} entrées.
"""


def call_openai(cfg: dict, topic: Topic) -> dict:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise Fatal("le paquet « openai » n'est pas installé (pip install openai).") from exc

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise Fatal(
            "la variable d'environnement OPENAI_API_KEY est vide. "
            "Sur GitHub : Settings > Secrets and variables > Actions > New repository secret.")

    model = cfg.get("openai_model", "gpt-4o-mini")
    temperature = cfg.get("openai_temperature", 0.7)
    faq_count = int(cfg["faq_questions_count"])

    system = SYSTEM_PROMPT.format(
        site_name=cfg["site_name"],
        sector=cfg.get("sector", ""),
        location=cfg.get("location", ""),
        language="français" if cfg.get("language") == "fr" else cfg.get("language", "français"),
        tone=cfg.get("tone", ""),
        geo_keywords=", ".join(cfg.get("geo_keywords", [])),
    )
    user = USER_PROMPT.format(
        number=topic.number,
        subject=topic.subject,
        angle=topic.angle,
        word_count=cfg.get("target_word_count", 1300),
        faq_count=faq_count,
    )

    step(f"appel OpenAI — modèle {model}, temperature {temperature}")
    client = OpenAI(api_key=api_key, timeout=180.0, max_retries=3)
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=temperature,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
    except Exception as exc:                            # noqa: BLE001 — on veut TOUT attraper
        raise Fatal(f"l'appel OpenAI a échoué ({type(exc).__name__}) : {exc}") from exc

    usage = getattr(response, "usage", None)
    if usage:
        ok(f"réponse reçue — {usage.prompt_tokens} tokens en entrée, "
           f"{usage.completion_tokens} en sortie")

    raw = (response.choices[0].message.content or "").strip()
    if not raw:
        raise Fatal("OpenAI a renvoyé une réponse vide.")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Fatal(f"la réponse d'OpenAI n'est pas du JSON valide : {exc}") from exc


# ─────────────────────────────────────────────────────────────────────────────
# Validation & normalisation de la réponse
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Article:
    topic: Topic
    title: str
    slug: str
    meta_description: str
    card_excerpt: str
    rss_description: str
    category: str
    body_html: str
    faq: list[tuple[str, str]]
    cta_title: str
    cta_text: str
    image_src: str
    image_alt: str
    word_count: int
    reading_minutes: int


ALLOWED_CATEGORIES = {"Cuisine", "Agencement", "Salle de bains", "Dressing", "Éclairage"}
FORBIDDEN_IN_BODY = re.compile(r"<\s*(script|style|iframe|img|h1|table|form)\b", re.I)


def clean_body(body: str) -> str:
    """Filet de sécurité : on retire ce que le prompt interdit déjà."""
    body = re.sub(r"^```(?:html)?\s*|\s*```$", "", body.strip())
    body = re.sub(r"<script\b.*?</script>", "", body, flags=re.S | re.I)
    body = re.sub(r"<style\b.*?</style>", "", body, flags=re.S | re.I)
    body = re.sub(r"</?h1\b[^>]*>", lambda m: "<h2>" if not m.group(0).startswith("</") else "</h2>",
                  body, flags=re.I)
    return body.strip()


def count_words(body_html: str) -> int:
    return len(re.sub(r"<[^>]+>", " ", body_html).split())


def build_article(cfg: dict, topic: Topic, data: dict, existing_slugs: set[str]) -> Article:
    step("validation de la réponse")

    def field(name: str) -> str:
        value = data.get(name)
        if not isinstance(value, str) or not value.strip():
            raise Fatal(f"champ « {name} » manquant ou vide dans la réponse OpenAI.")
        return typo(value.strip())

    title = field("title")
    meta_description = field("meta_description")
    if len(meta_description) > 155:
        warn(f"meta description trop longue ({len(meta_description)} car.) — troncature à 155")
        meta_description = meta_description[:152].rstrip(" ,;:.") + "…"

    slug = slugify(data.get("slug") or title)
    if not slug:
        raise Fatal("impossible de dériver un slug exploitable.")
    if slug in existing_slugs:
        raise NothingToDo(
            f"l'article /blog/{slug}/ existe déjà — aucun fichier n'a été touché.")
    if (ROOT / "blog" / slug).exists():
        raise NothingToDo(
            f"le dossier blog/{slug} existe déjà — aucun fichier n'a été touché.")

    body_html = clean_body(field("body_html"))
    if FORBIDDEN_IN_BODY.search(body_html):
        raise Fatal("le corps de l'article contient une balise interdite après nettoyage.")
    words = count_words(body_html)
    if words < 600:
        raise Fatal(f"article trop court ({words} mots) — génération rejetée, rien n'a été écrit.")
    if words < 1000:
        warn(f"article un peu court : {words} mots (cible {cfg.get('target_word_count', 1300)})")

    raw_faq = data.get("faq")
    if not isinstance(raw_faq, list) or not raw_faq:
        raise Fatal("la FAQ est absente de la réponse OpenAI.")
    faq: list[tuple[str, str]] = []
    for item in raw_faq:
        if not isinstance(item, dict):
            continue
        question = typo(str(item.get("question", "")).strip())
        answer = typo(re.sub(r"<[^>]+>", "", str(item.get("answer", ""))).strip())
        if question and answer:
            faq.append((question, answer))
    expected = int(cfg["faq_questions_count"])
    if len(faq) < 4:
        raise Fatal(f"FAQ inexploitable : {len(faq)} question(s) valide(s), minimum 4.")
    if len(faq) != expected:
        warn(f"FAQ de {len(faq)} questions au lieu de {expected} — conservée telle quelle")

    category = data.get("category", "").strip()
    if category not in ALLOWED_CATEGORIES:
        category = "Cuisine"

    image = cfg.get("topic_images", {}).get(str(topic.number)) or cfg.get("default_image") or {}
    image_src = image.get("src", "/assets/cuisine-1.jpg")
    image_alt = typo(image.get("alt", "Réalisation ADesign à Tarbes"))
    if not (ROOT / image_src.lstrip("/")).exists():
        warn(f"image {image_src} absente du dépôt — repli sur /assets/cuisine-1.jpg")
        image_src = "/assets/cuisine-1.jpg"

    ok(f"slug         : {slug}")
    ok(f"titre        : {title}")
    ok(f"meta descr.  : {len(meta_description)} caractères")
    ok(f"corps        : {words} mots, {body_html.count('<h2>')} H2, {body_html.count('<h3>')} H3")
    ok(f"FAQ          : {len(faq)} questions")

    return Article(
        topic=topic,
        title=title,
        slug=slug,
        meta_description=meta_description,
        card_excerpt=field("card_excerpt")[:220],
        rss_description=field("rss_description")[:280],
        category=category,
        body_html=body_html,
        faq=faq,
        cta_title=field("cta_title"),
        cta_text=field("cta_text"),
        image_src=image_src,
        image_alt=image_alt,
        word_count=words,
        reading_minutes=max(5, round(words / 200)),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rendu de l'article — à partir du gabarit relu à chaque exécution
# ─────────────────────────────────────────────────────────────────────────────

def render_article(cfg: dict, art: Article, today: datetime) -> str:
    step("fabrication du HTML à partir du gabarit")

    template_path = ROOT / cfg["reference_article"]
    if not template_path.exists():
        raise Fatal(f"gabarit introuvable : {cfg['reference_article']}")
    doc = read(template_path)

    base = cfg["site_url"]
    url = f"{base}/blog/{art.slug}/"
    iso = today.strftime("%Y-%m-%d")
    image_url = f"{base}{art.image_src}"
    title_tag = f"{art.title} | {cfg['site_name']}"

    # ── <head> ────────────────────────────────────────────────────────────
    doc = sub_once(r"<title>.*?</title>", f"<title>{attr(title_tag)}</title>",
                   doc, "la balise <title>", re.S)
    doc = sub_once(r'<meta name="description"\s*\n?\s*content=".*?"\s*/>',
                   '<meta name="description"\n        content="'
                   + attr(art.meta_description) + '" />',
                   doc, 'la meta description', re.S)
    doc = sub_once(r'<link rel="canonical" href=".*?" />',
                   f'<link rel="canonical" href="{url}" />', doc, "le canonical")

    for name, value in (
        ('name="article:published_time"', iso),
        ('property="article:published_time"', iso),
        ('property="og:title"', art.title),
        ('property="og:description"', art.meta_description),
        ('property="og:url"', url),
        ('property="og:image"', image_url),
        ('property="og:image:alt"', art.image_alt),
        ('property="article:section"', art.category),
        ('name="twitter:title"', art.title),
        ('name="twitter:description"', art.meta_description),
        ('name="twitter:image"', image_url),
        ('name="twitter:image:alt"', art.image_alt),
    ):
        doc = sub_once(rf'<meta {re.escape(name)} content=".*?" />',
                       f'<meta {name} content="{attr(value)}" />',
                       doc, f"la balise meta {name}")

    # ── JSON-LD (3 blocs, dans l'ordre : Article, BreadcrumbList, FAQPage) ─
    blocks = re.findall(r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
                        doc, re.S)
    if len(blocks) != 3:
        raise Fatal(f"gabarit inattendu : {len(blocks)} bloc(s) JSON-LD au lieu de 3.")

    try:
        article_ld = json.loads(blocks[0])
        breadcrumb_ld = json.loads(blocks[1])
        faq_ld = json.loads(blocks[2])
    except json.JSONDecodeError as exc:
        raise Fatal(f"le JSON-LD du gabarit est invalide : {exc}") from exc

    article_ld.update({
        "headline": art.title,
        "description": art.meta_description,
        "image": [image_url],
        "datePublished": iso,
        "dateModified": iso,
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "articleSection": art.category,
    })
    breadcrumb_ld["itemListElement"][2].update({"name": art.title, "item": url})
    faq_ld["mainEntity"] = [
        {"@type": "Question", "name": question,
         "acceptedAnswer": {"@type": "Answer", "text": answer}}
        for question, answer in art.faq
    ]

    def dump(obj: dict) -> str:
        body = json.dumps(obj, ensure_ascii=False, indent=2)
        return "\n".join("  " + line for line in body.splitlines())

    for old, new in zip(blocks, (article_ld, breadcrumb_ld, faq_ld)):
        doc = doc.replace(f'<script type="application/ld+json">\n  {old}\n  </script>',
                          f'<script type="application/ld+json">\n{dump(new)}\n  </script>', 1)

    # ── corps ─────────────────────────────────────────────────────────────
    doc = sub_once(r'<li><span aria-current="page">.*?</span></li>',
                   f'<li><span aria-current="page">{html.escape(art.title)}</span></li>',
                   doc, "le fil d'Ariane", re.S)
    doc = sub_once(r"<h1>.*?</h1>", f"<h1>{html.escape(art.title)}</h1>",
                   doc, "le <h1>", re.S)
    doc = sub_once(r'<time datetime="[^"]*">[^<]*</time>',
                   f'<time datetime="{iso}">{date_fr(today)}</time>',
                   doc, "la date affichée")
    doc = sub_once(r"<span>Lecture[^<]*</span>",
                   f"<span>Lecture : {art.reading_minutes} min</span>",
                   doc, "la durée de lecture")
    doc = sub_once(r'<img class="post-cover"[^>]*/>',
                   f'<img class="post-cover" src="{art.image_src}" alt="{attr(art.image_alt)}"'
                   ' width="1200" height="700" />',
                   doc, "l'image d'en-tête")

    body = "\n".join("        " + line if line.strip() else ""
                     for line in art.body_html.splitlines())
    doc = sub_once(r'(<div class="post-body">)(.*?)(\n\s*</div>\n\n\s*<!-- FAQ -->)',
                   f'<div class="post-body">\n\n{body}\n\n      </div>\n\n      <!-- FAQ -->',
                   doc, "le bloc .post-body", re.S)

    faq_items = "\n\n".join(
        '        <div class="faq-item">\n'
        f'          <h3>{html.escape(question)}</h3>\n'
        f'          <p>{html.escape(answer)}</p>\n'
        '        </div>'
        for question, answer in art.faq)
    doc = sub_once(r'(<section class="faq" aria-labelledby="faq-title">)(.*?)(</section>)',
                   '<section class="faq" aria-labelledby="faq-title">\n'
                   '        <h2 id="faq-title">Questions fréquentes</h2>\n\n'
                   f'{faq_items}\n      </section>',
                   doc, "le bloc .faq", re.S)

    doc = sub_once(r'(<aside class="post-cta">)(.*?)(</aside>)',
                   '<aside class="post-cta">\n'
                   f'        <h2>{html.escape(art.cta_title)}</h2>\n'
                   f'        <p>{html.escape(art.cta_text)}</p>\n'
                   '        <a class="btn" href="/contact.html">DEMANDER UN DEVIS</a>\n'
                   '      </aside>',
                   doc, "le bloc .post-cta", re.S)

    # Marqueur de sujet : c'est lui qui garantit l'idempotence des exécutions.
    doc = doc.replace("<!doctype html>",
                      "<!doctype html>\n" + TOPIC_MARKER.format(n=art.topic.number), 1)

    verify(doc, art)
    ok("HTML de l'article validé")
    return doc


def verify(doc: str, art: Article) -> None:
    """Contrôles de BLOG_WORKFLOW.md § 5, avant toute écriture."""
    for index, block in enumerate(
            re.findall(r'<script type="application/ld\+json">(.*?)</script>', doc, re.S)):
        try:
            json.loads(block)
        except json.JSONDecodeError as exc:
            raise Fatal(f"JSON-LD n°{index + 1} invalide dans l'article généré : {exc}") from exc

    described = re.search(r'name="description"[^>]*content="(.*?)"', doc, re.S)
    if not described or len(html.unescape(described.group(1))) > 155:
        raise Fatal("la meta description dépasse 155 caractères dans le HTML final.")
    if doc.count("<h1>") != 1:
        raise Fatal(f"{doc.count('<h1>')} balise(s) <h1> dans l'article — il en faut exactement une.")

    # Le JSON-LD FAQPage doit reprendre mot pour mot les Q/R visibles.
    visible = re.findall(r'<div class="faq-item">\s*<h3>(.*?)</h3>\s*<p>(.*?)</p>', doc, re.S)
    if len(visible) != len(art.faq):
        raise Fatal("divergence entre la FAQ visible et la FAQ attendue.")
    for (question, answer), (vq, va) in zip(art.faq, visible):
        if html.unescape(vq) != question or html.unescape(va) != answer:
            raise Fatal("le texte visible de la FAQ diverge du JSON-LD FAQPage.")


# ─────────────────────────────────────────────────────────────────────────────
# Mises à jour des fichiers d'index (insertions, jamais de réécriture)
# ─────────────────────────────────────────────────────────────────────────────

def update_blog_index(cfg: dict, art: Article, today: datetime) -> str:
    step("insertion de la card dans blog/index.html")
    path = ROOT / "blog" / "index.html"
    doc = read(path)
    url = f"/blog/{art.slug}/"

    if url in doc:
        warn("la card existe déjà — blog/index.html laissé intact")
        return doc

    card = f"""        <!-- ══ ARTICLE ══ -->
        <article class="post-card">
          <a href="{url}" aria-label="Lire : {attr(art.title)}">
            <img class="thumb" src="{art.image_src}" alt="{attr(art.image_alt)}" width="800" height="600" loading="lazy" />
          </a>
          <div class="content">
            <p class="meta"><time datetime="{today:%Y-%m-%d}">{date_fr(today)}</time></p>
            <h2><a href="{url}">{html.escape(art.title)}</a></h2>
            <p>{html.escape(art.card_excerpt)}</p>
            <a class="btn" href="{url}">LIRE L’ARTICLE</a>
          </div>
        </article>
        <!-- ══ FIN ARTICLE ══ -->

"""
    marker = "        <!-- ══ ARTICLE ══ -->"
    if marker not in doc:
        raise Fatal("marqueur « <!-- ══ ARTICLE ══ --> » introuvable dans blog/index.html.")
    doc = doc.replace(marker, card + marker, 1)     # la plus récente en tête

    # JSON-LD Blog : on insère dans le tableau blogPost sans reformater le reste.
    entry = (f'\n      {{\n'
             f'        "@type": "BlogPosting",\n'
             f'        "headline": {json.dumps(art.title, ensure_ascii=False)},\n'
             f'        "url": "{cfg["site_url"]}{url}",\n'
             f'        "datePublished": "{today:%Y-%m-%d}",\n'
             f'        "author": {{ "@type": "Organization", "name": "{cfg["author"]}" }}\n'
             f'      }},')
    doc = sub_once(r'"blogPost":\s*\[', '"blogPost": [' + entry, doc,
                   "le tableau blogPost du JSON-LD de blog/index.html")

    cards = doc.count('<article class="post-card">')
    if cards > int(cfg.get("max_cards_on_blog_index", 6)):
        warn(f"{cards} cards sur /blog/ : il est temps de paginer "
             "(voir BLOG_WORKFLOW.md § 3). Le site reste valide en attendant.")
    return doc


def update_sitemap(cfg: dict, art: Article, today: datetime) -> str:
    step("mise à jour de sitemap.xml")
    path = ROOT / "sitemap.xml"
    doc = read(path)
    loc = f"{cfg['site_url']}/blog/{art.slug}/"
    iso = today.strftime("%Y-%m-%d")

    if loc in doc:
        warn("l'URL est déjà dans le sitemap — fichier laissé intact")
        return doc

    blog_loc = f'<loc>{cfg["site_url"]}/blog/</loc>'
    doc = sub_once(rf'{re.escape(blog_loc)}\s*<lastmod>[^<]*</lastmod>',
                   f"{blog_loc}\n    <lastmod>{iso}</lastmod>",
                   doc, "le lastmod de /blog/ dans le sitemap", re.S)

    entry = (f"  <url>\n    <loc>{loc}</loc>\n    <lastmod>{iso}</lastmod>\n"
             f"    <changefreq>yearly</changefreq>\n    <priority>0.8</priority>\n  </url>\n\n")
    return sub_once(r"\n</urlset>", "\n" + entry + "</urlset>", doc,
                    "la fermeture </urlset> du sitemap")


def update_rss(cfg: dict, art: Article, today: datetime) -> str:
    step("insertion de l'item dans blog/rss.xml")
    path = ROOT / "blog" / "rss.xml"
    doc = read(path)
    link = f"{cfg['site_url']}/blog/{art.slug}/"

    if link in doc:
        warn("l'item existe déjà dans le flux RSS — fichier laissé intact")
        return doc

    pub_date = date_rfc822(today)
    doc = sub_once(r"<lastBuildDate>[^<]*</lastBuildDate>",
                   f"<lastBuildDate>{pub_date}</lastBuildDate>",
                   doc, "le lastBuildDate du flux RSS")

    item = f"""    <item>
      <title>{xml_escape(art.title)}</title>
      <link>{link}</link>
      <guid isPermaLink="true">{link}</guid>
      <pubDate>{pub_date}</pubDate>
      <author>{xml_escape(cfg.get("email", ""))} ({cfg["author"]})</author>
      <category>{xml_escape(art.category)}</category>
      <description>{xml_escape(art.rss_description)}</description>
    </item>

"""
    return sub_once(r"\n    <item>", "\n" + item + "    <item>", doc,
                    "le premier <item> du flux RSS")


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> int:
    today = datetime.now(timezone.utc)

    log("═" * 72)
    log(f"  ADesign — génération d'article  ·  {today:%Y-%m-%d %H:%M} UTC"
        + ("  ·  DRY-RUN" if args.dry_run else ""))
    log("═" * 72)

    cfg = load_config()
    ok(f"config chargée — {cfg['site_name']}, {cfg.get('location', '')}")

    topics = load_topics(cfg)
    ok(f"{len(topics)} sujets lus dans BLOG_WORKFLOW.md § 7")

    done, slugs = scan_existing()
    ok(f"{len(slugs)} article(s) déjà en ligne, {len(done)} sujet(s) de la liste traité(s)")

    topic = pick_topic(topics, done, args.topic)
    step(f"sujet retenu : #{topic.number} — {topic.subject}")
    log(f"  angle : {topic.angle}")

    data = call_openai(cfg, topic)
    art = build_article(cfg, topic, data, slugs)

    article_html = render_article(cfg, art, today)
    blog_index = update_blog_index(cfg, art, today)
    sitemap = update_sitemap(cfg, art, today)
    rss = update_rss(cfg, art, today)

    if args.dry_run:
        log()
        log("─" * 72)
        log("  DRY-RUN — aucun fichier écrit, aucun commit")
        log("─" * 72)
        log(f"Fichier qui serait créé : blog/{art.slug}/index.html "
            f"({len(article_html)} octets)")
        log("Fichiers qui seraient modifiés : blog/index.html, sitemap.xml, blog/rss.xml")
        log()
        log(f"TITRE : {art.title}")
        log(f"SLUG  : {art.slug}")
        log(f"META  : {art.meta_description}  ({len(art.meta_description)} car.)")
        log(f"MOTS  : {art.word_count}  ·  lecture {art.reading_minutes} min "
            f"·  catégorie {art.category}")
        log()
        log("─── 200 premiers mots ───")
        plain = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", art.body_html)))
        log(" ".join(plain.split()[:200]) + " […]")
        log()
        log("─── FAQ ───")
        for i, (question, answer) in enumerate(art.faq, 1):
            log(f"{i}. {question}")
            log(f"   {answer}")
        log()
        log(f"─── CTA ───\n{art.cta_title}\n{art.cta_text}")
        log()
        if args.out:
            Path(args.out).write_text(article_html, encoding="utf-8")
            ok(f"HTML complet écrit hors dépôt : {args.out}")
        log("Dry-run terminé.")
        return EXIT_OK

    step("écriture des fichiers")
    directory = ROOT / "blog" / art.slug
    directory.mkdir(parents=True, exist_ok=False)      # échoue si déjà présent
    (directory / "index.html").write_text(article_html, encoding="utf-8")
    (ROOT / "blog" / "index.html").write_text(blog_index, encoding="utf-8")
    (ROOT / "sitemap.xml").write_text(sitemap, encoding="utf-8")
    (ROOT / "blog" / "rss.xml").write_text(rss, encoding="utf-8")

    ok(f"blog/{art.slug}/index.html créé")
    ok("blog/index.html, sitemap.xml et blog/rss.xml mis à jour")
    log()
    log(f"✅ Article publié : {cfg['site_url']}/blog/{art.slug}/")
    log(f"   « {art.title} » — {art.word_count} mots, {len(art.faq)} questions de FAQ")

    # Consommé par le workflow GitHub Actions pour le message de commit.
    if summary := os.environ.get("GITHUB_OUTPUT"):
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"slug={art.slug}\ntitle={art.title}\n")

    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description="Génère un article de blog ADesign.")
    parser.add_argument("--dry-run", action="store_true",
                        help="génère et valide tout, sans écrire ni committer")
    parser.add_argument("--topic", type=int, metavar="N",
                        help="force le sujet N de BLOG_WORKFLOW.md § 7")
    parser.add_argument("--out", metavar="FICHIER",
                        help="en dry-run, écrit le HTML complet à ce chemin (hors dépôt)")
    args = parser.parse_args()

    try:
        return run(args)
    except NothingToDo as exc:
        log()
        log(f"⏭️  Rien à faire : {exc}")
        return EXIT_NOTHING_TO_DO
    except Fatal as exc:
        log()
        log(f"❌ Échec : {exc}")
        return EXIT_ERROR
    except Exception as exc:                            # noqa: BLE001 — filet final
        log()
        log(f"❌ Erreur inattendue ({type(exc).__name__}) : {exc}")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

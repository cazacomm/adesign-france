# Génération automatique d'articles — ADesign

`generate-article.py` publie un article de blog complet : page HTML, carte sur
`/blog/`, entrée de `sitemap.xml`, item RSS et ligne dans `llms.txt`.

Il est appelé chaque lundi par `.github/workflows/blog-auto.yml`, et peut se
lancer à la main.

---

## Le principe : le modèle écrit, le script assemble

L'API OpenAI ne renvoie **que du contenu éditorial, en JSON** — titre, chapô,
sections, listes, FAQ. Elle ne produit pas une ligne de HTML.

Tout le reste est fabriqué par le script à partir du gabarit : `<head>`, meta,
canonical, Open Graph, Twitter Card, les trois blocs JSON-LD, le fil d'Ariane,
le marqueur d'idempotence, le header et le footer.

C'est ce qui débloque le volume. Quand le modèle régénérait la page entière,
les deux tiers de ses tokens de sortie partaient en balisage et le corps
plafonnait autour de 850 mots quelle que soit la consigne. En ne lui demandant
que du texte, la cible de 1200-1500 mots redevient tenable.

Effet de bord utile : le canonical, les JSON-LD et la structure ne peuvent plus
être erronés, puisqu'ils ne dépendent plus du modèle. `json.dumps` produit un
JSON-LD valide par construction.

---

## Le gabarit vient d'un article existant

Le script **relit un article publié** (`reference_article_slug`) et en extrait
les morceaux réutilisables. Il n'y a aucun template HTML en dur : si le design
du blog évolue, il suffit de mettre à jour l'article de référence, et les
suivants héritent du changement.

`split_template()` est la fonction adaptée aux conventions HTML **de ce
site-ci**, qui diffèrent de celles du pipeline d'origine :

| Convention ADesign | Ce que le parseur fait |
|---|---|
| `<!doctype html>` en minuscules | contrôle insensible à la casse |
| pas de balise `<main>` | le corps est un `<article class="post">` + `<div class="container">` |
| JSON-LD précédés de commentaires encadrés « DONNÉES STRUCTURÉES » | repère le premier `<script type="application/ld+json">` puis remonte au commentaire qui l'introduit |
| fil d'Ariane hors de l'article, dans son propre `<div class="container">` | découpe le header à `<!-- FIL D'ARIANE -->` |
| FAQ en `<div class="faq-item">` (h3 + p) | génère et compte ces blocs |
| CTA en `<aside class="post-cta">`, suivi de `<p class="post-back">` | les reprend tels quels du gabarit |
| meta description écrite sur deux lignes | motif tolérant le retour à la ligne |
| flux RSS dans `/blog/rss.xml` | chemin adapté (et non à la racine) |

**Le gabarit HTML n'est jamais modifié — c'est le parseur qui s'adapte à lui.**

Même logique pour `BLOG_WORKFLOW.md`, que le script lit sans jamais l'écrire :
les sujets y sont présentés en **tableau markdown** (`| # | Sujet | Angle |`) et
non en liste numérotée, et les règles éditoriales vivent sous le titre
« Contenu — les règles à ne pas franchir ». `parse_topics()` lit le tableau en
priorité et retombe sur la liste numérotée si le document change de forme.

---

## Idempotence

Trois verrous, dans cet ordre :

1. **Marqueur** — chaque page publiée porte `<!-- adesign-topic: N -->` juste
   après `<body>`. Au démarrage, le script scanne `/blog/*/index.html` et
   considère ces sujets comme traités.
2. **Collision de slug** — si le dossier `/blog/<slug>/` existe déjà, le sujet
   est sauté. Les slugs sont déterministes : même titre, même slug.
3. **URL déjà présente** — chaque mise à jour annexe (index, sitemap, RSS,
   `llms.txt`) vérifie l'URL avant d'insérer. Relancer le script n'ajoute
   jamais de doublon.

Un article déjà en ligne n'est jamais écrasé : il faut `--rewrite` pour cela.
L'article rédigé à la main avant la mise en place du pipeline ne porte pas de
marqueur, mais son slug protège le sujet correspondant.

---

## Volume et rattrapage

| Réglage | Valeur |
|---|---|
| Cible annoncée au modèle | 1200-1500 mots |
| Bornes de validation | 900-1900 mots |
| Appels OpenAI maximum | **3**, rattrapages compris |
| Modèle | `gpt-4o` |

Le comptage porte sur le **contenu** (chapô + sections, FAQ exclue), pas sur le
HTML : plus de balises ni de boilerplate dans le total.

Le rattrapage ne se déclenche pas que sur le volume : **toute erreur de
validation que le modèle peut corriger lui-même** — maillage interne absent,
nombre de questions faux, `title` hors bornes — relance une passe tant qu'il
reste du budget.

Chaque reprise repart de la **meilleure copie obtenue**, jamais de la dernière :
le modèle développe alors un texte déjà long au lieu de repartir d'un plus
court. L'arbitrage se fait sur `(nombre d'erreurs, écart à la cible)` — compter
les erreurs plutôt que constater leur présence permet de départager deux copies
toutes deux invalides.

Le plafond de 3 appels borne le coût : un article coûte au pire trois appels
`gpt-4o`, et l'échec est propre — aucun fichier n'est écrit.

---

## Garde-fous éditoriaux

Le prompt interdit explicitement d'inventer prix, fourchettes budgétaires,
chiffres d'affaires, noms de clients, délais chiffrés, dates de fondation,
normes, réglementations, dispositifs d'aide, labels, avis clients, horaires et
adresses.

La seule source autorisée de faits est la liste `facts` de `blog-config.json`,
tirée du contenu réel du site. Pour élargir ce que le modèle a le droit
d'affirmer, il faut ajouter un fait vérifié à cette liste — jamais assouplir le
prompt.

Deux protections structurelles complètent l'instruction :

- **Aucune injection HTML possible** : le modèle ne renvoie que du texte, qui
  passe par `esc()` avant d'atteindre la page.
- **Aucun lien externe possible** : le seul balisage inline accepté est
  `**gras**` et `[libellé](/chemin)`, et le motif exige un chemin commençant
  par `/`. Un lien sortant ne peut pas franchir le parseur.

Le maillage interne est vérifié à chaque passe : au moins deux liens vers les
`internal_link_targets` dans deux sections différentes, plus un lien vers
`/blog/`.

---

## Utilisation

```bash
# Simulation complète, sans appel API ni écriture — le test de référence
python3 scripts/generate-article.py --mock --dry-run

# Vrai appel OpenAI, mais rien n'est écrit
export OPENAI_API_KEY="sk-..."
python3 scripts/generate-article.py --dry-run

# Publication du prochain sujet non traité
python3 scripts/generate-article.py

# Régénération d'un article existant (écrase le fichier)
python3 scripts/generate-article.py --rewrite mon-slug-existant
```

`--mock` et `--dry-run` se combinent : c'est le moyen de vérifier l'assemblage
après une modification du gabarit, sans dépenser un centime.

### Codes de sortie

| Code | Sens |
|---|---|
| `0` | succès |
| `78` | aucun sujet restant, ou l'article existe déjà — arrêt propre |
| `1` | erreur ; **aucun fichier n'a été écrit** |

L'écriture n'a lieu qu'une fois le contenu validé **et** l'assemblage vérifié.
Un échec ne laisse jamais le dépôt à moitié modifié.

---

## Déclenchement manuel du workflow

Onglet **Actions** → *Blog auto — ADesign* → **Run workflow**. Deux champs :

- **dry_run** — génère l'article et affiche le résultat sans rien écrire ni
  pousser. Utile pour vérifier que la clé API répond.
- **rewrite** — slug d'un article existant à régénérer. Laisser vide pour
  publier le sujet suivant.

Le secret `OPENAI_API_KEY` doit être défini dans *Settings → Secrets and
variables → Actions*.

---

## Configuration — `blog-config.json`

Les 19 clés sont **toutes obligatoires** : `load_config()` échoue au démarrage
si l'une manque ou est vide, plutôt que de produire une page incomplète.

| Clé | Rôle |
|---|---|
| `site_name`, `site_url` | identité et base des URL absolues |
| `sector`, `location` | contexte métier injecté dans le prompt |
| `geo_keywords` | ancrage local ; les 6 premiers alimentent `keywords` du JSON-LD |
| `tone`, `author`, `language` | ligne éditoriale et signature |
| `target_word_count`, `faq_questions_count` | calibrage du contenu |
| `model`, `temperature` | paramètres OpenAI |
| `topic_marker_prefix` | préfixe du marqueur d'idempotence (`adesign-topic`) |
| `og_image` | image de partage et photo d'en-tête des articles |
| `logo_path` | logo dans le `publisher` du JSON-LD |
| `default_article_section` | catégorie éditoriale (`Cuisine`) |
| `internal_link_targets` | les 3 chemins du maillage interne obligatoire |
| `reference_article_slug` | l'article qui sert de gabarit |
| `facts` | **seule** source de faits chiffrés, d'adresses et d'horaires |

### Ajouter un sujet

Ajouter une ligne au tableau de la section « Sujets suggérés » de
`BLOG_WORKFLOW.md` :

```markdown
| 13 | Titre de l'article | Angle éditorial en quelques mots |
```

Le script prend les sujets dans l'ordre des numéros et s'arrête proprement
(code 78) quand la liste est épuisée.

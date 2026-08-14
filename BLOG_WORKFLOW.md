# Blog ADesign — mode d'emploi

Le site est en **HTML/CSS statique pur**, déployé sur GitHub Pages
(`cazacomm/adesign-france`, domaine `adesign-france.fr`). Il n'y a ni générateur
de site, ni front-matter, ni build : **chaque article est un fichier HTML écrit
à la main**. Ce document décrit la procédure exacte pour en ajouter un.

---

## 1. Où vivent les fichiers

```
/blog/
  index.html                 ← liste des articles (les cards)
  rss.xml                    ← flux RSS
  <slug-de-l-article>/
    index.html               ← l'article
/assets/
  blog.css                   ← CSS commun à TOUTES les pages du blog
  <image-article>.jpg        ← visuels
/sitemap.xml                 ← à la racine
/robots.txt                  ← à la racine
/llms.txt                    ← à la racine
```

Le blog utilise **`/assets/blog.css`**, une feuille partagée. Les 10 pages
historiques du site gardent leur CSS inline et ne dépendent pas de ce fichier :
modifier `blog.css` ne peut donc **pas** casser le site existant.

### Règle sur les chemins

Dans `/blog/`, **tous les liens et images doivent être en chemin absolu**
(`/index.html`, `/assets/logo-adesign.png`, `/cuisine.html`). Les pages
historiques utilisent des chemins relatifs (`index.html`) : ne les copiez pas
tels quels dans un article, ils pointeraient vers `/blog/<slug>/index.html`.

---

## 2. Ajouter un article — les 5 étapes

### Étape 1 — Choisir le slug

Minuscules, tirets, sans accent ni ponctuation, mots-clés en tête.

```
Titre : Quel plan de travail choisir pour sa cuisine ?
Slug  : quel-plan-de-travail-choisir-pour-sa-cuisine
URL   : https://adesign-france.fr/blog/quel-plan-de-travail-choisir-pour-sa-cuisine/
```

Un slug ne se change **jamais** après publication (l'URL serait cassée). En cas
d'absolue nécessité, garder l'ancien dossier avec une redirection meta refresh.

### Étape 2 — Créer le fichier

```bash
mkdir -p blog/<slug>
cp blog/comment-choisir-sa-cuisine-equipee-a-tarbes-en-2026/index.html \
   blog/<slug>/index.html
```

Le premier article sert de **gabarit de référence**. Dans la copie, remplacer :

| À remplacer | Où | Remarque |
|---|---|---|
| `<title>` | `<head>` | ~55-60 caractères, finir par `\| ADesign` |
| `<meta name="description">` | `<head>` | **155 caractères maximum**, unique sur tout le site |
| `<link rel="canonical">` | `<head>` | URL complète avec le `/` final |
| `og:title`, `og:description`, `og:url`, `og:image` | `<head>` | |
| `twitter:title`, `twitter:description`, `twitter:image` | `<head>` | |
| `article:published_time` | `<head>` | format `AAAA-MM-JJ` |
| JSON-LD **Article** | `<head>` | `headline`, `description`, `image`, `datePublished`, `dateModified`, `mainEntityOfPage` |
| JSON-LD **BreadcrumbList** | `<head>` | 3ᵉ élément : nom + URL de l'article |
| JSON-LD **FAQPage** | `<head>` | doit reprendre **mot pour mot** les Q/R du bloc `.faq` |
| `<h1>` | corps | un seul H1 par page |
| `<time datetime="...">` + date affichée | corps | |
| `.post-cover` (image d'en-tête) | corps | `alt` descriptif obligatoire |
| Contenu `.post-body` | corps | |
| Bloc `.faq` | corps | |

> **Le JSON-LD FAQPage doit être strictement identique au texte visible de la
> FAQ.** Une divergence entre les deux est une raison de rejet par Google.

### Étape 3 — Ajouter la card dans `/blog/index.html`

Dupliquer le bloc entre `<!-- ══ ARTICLE ══ -->` et `<!-- ══ FIN ARTICLE ══ -->`,
**en haut** de `.post-grid` (les articles sont du plus récent au plus ancien),
puis adapter lien, image, `alt`, date, titre et extrait (1 à 2 phrases).

Ajouter aussi l'article dans le tableau `blogPost` du JSON-LD `Blog` en haut de
la page.

### Étape 4 — Mettre à jour `sitemap.xml`

Dupliquer le dernier bloc `<url>` de la section `<!-- ══ BLOG ══ -->` :

```xml
  <url>
    <loc>https://adesign-france.fr/blog/<slug>/</loc>
    <lastmod>AAAA-MM-JJ</lastmod>
    <changefreq>yearly</changefreq>
    <priority>0.8</priority>
  </url>
```

Passer aussi le `<lastmod>` de `https://adesign-france.fr/blog/` à la date du
jour.

### Étape 5 — Mettre à jour `blog/rss.xml`

Insérer un `<item>` **en première position** de la liste :

```xml
    <item>
      <title>Titre de l'article</title>
      <link>https://adesign-france.fr/blog/<slug>/</link>
      <guid isPermaLink="true">https://adesign-france.fr/blog/<slug>/</guid>
      <pubDate>Thu, 14 Aug 2026 09:00:00 +0200</pubDate>
      <author>adesign-france@adesign-france.fr (ADesign)</author>
      <category>Cuisine</category>
      <description>Résumé en 1-2 phrases.</description>
    </item>
```

Le `<pubDate>` suit le format **RFC-822** (`Jour, JJ Mois AAAA HH:MM:SS +0200`),
qui n'est pas le même que le format ISO du sitemap. Mettre à jour
`<lastBuildDate>` avec la même valeur.

**Optionnel mais recommandé :** ajouter une ligne dans la section `## Blog` de
`/llms.txt`, avec une description factuelle de ce que couvre l'article — c'est
ce fichier que lisent les moteurs de réponse génératifs.

---

## 3. Pagination

La liste affiche tous les articles tant qu'il y en a **6 ou moins**. À partir du
7ᵉ :

1. Ne garder que les 6 articles les plus récents dans `/blog/index.html`.
2. Créer `/blog/page/2/index.html` — copie de `/blog/index.html` avec les
   articles 7 à 12, un `<title>` suffixé « — page 2 », un canonical
   `https://adesign-france.fr/blog/page/2/` et le `<h1>` inchangé.
3. Décommenter le bloc `<nav class="pagination">` déjà présent en bas de
   `/blog/index.html`, et l'adapter sur chaque page.
4. Ajouter chaque page de pagination au `sitemap.xml` (priorité `0.4`).

Le CSS `.pagination` est déjà écrit dans `blog.css` : rien à styler.

---

## 4. Images

- Déposer les visuels dans `/assets/` (le blog peut aussi réutiliser les images
  existantes du site, par exemple `/assets/cuisine-1.jpg`).
- Image d'en-tête : format paysage, **1200 px de large minimum**, en `.jpg`.
- Toujours renseigner un `alt` descriptif contenant si possible l'ancrage local.
- Nommer les fichiers en clair : `cuisine-ilot-tarbes.jpg`, pas `IMG_4821.jpg`.
- Sur les cards de la liste, garder `loading="lazy"`.

Générer une image au bon format :

```bash
sips --resampleWidth 1200 source.png -o /tmp/tmp.png
sips -c 630 1200 /tmp/tmp.png -s format jpeg -s formatOptions 82 \
     --out assets/mon-image.jpg
```

---

## 5. Vérifications avant publication

```bash
# 1. Le JSON-LD est-il valide ?
python3 -c "
import re,json,sys
s=open(sys.argv[1],encoding='utf-8').read()
for i,m in enumerate(re.findall(r'<script type=\"application/ld\+json\">(.*?)</script>',s,re.S)):
    json.loads(m); print('JSON-LD',i,'OK')
" blog/<slug>/index.html

# 2. Le XML est-il valide ?
python3 -c "import xml.dom.minidom as m;[m.parse(f) for f in ['sitemap.xml','blog/rss.xml']];print('XML OK')"

# 3. Longueur de la meta description (doit être <= 155)
python3 -c "
import re,sys
s=open(sys.argv[1],encoding='utf-8').read()
d=re.search(r'name=\"description\"[^>]*content=\"(.*?)\"',s,re.S).group(1)
print(len(d),'caractères')
" blog/<slug>/index.html

# 4. Aperçu local
python3 -m http.server 8000   # puis http://localhost:8000/blog/
```

Le serveur local est important : il reproduit les **chemins absolus**, ce qu'un
double-clic sur le fichier HTML ne fait pas.

Après mise en ligne, contrôler dans la Search Console que l'article est indexé,
et tester le balisage avec le test des résultats enrichis de Google.

---

## 6. Contenu — les règles à ne pas franchir

Ces règles protègent la crédibilité du site et évitent tout risque juridique.

**Interdit dans un article :**

- Prix, fourchettes de prix, pourcentages de remise.
- Chiffres précis présentés comme des faits (délais en jours, durées de
  garantie, statistiques de marché) s'ils ne sont pas vérifiés.
- Noms de clients, adresses de chantiers, témoignages non validés.
- Citation d'une norme, d'une réglementation ou d'un dispositif d'aide sans
  vérification directe à la source, avec la date de vérification.
- Dates de fondation, effectifs ou nombre de réalisations non confirmés par
  ADesign.
- Affirmations comparatives sur des concurrents nommés.

**Recommandé :**

- Conseil général + ancrage géographique : Tarbes, Hautes-Pyrénées, Bigorre,
  Lourdes, Bagnères-de-Bigorre, Vic-en-Bigorre, plaine de l'Adour.
- 1 200 à 1 500 mots, un seul H1, des H2 tous les 200-300 mots.
- Une FAQ de 4 à 6 questions reprenant de vraies questions de clients.
- 2 à 4 liens internes vers `/cuisine.html`, `/prestation-service.html`,
  `/contact.html` ou d'autres articles.
- Un bloc CTA `.post-cta` en fin d'article.
- Ton expert-conseil : on explique une méthode, on ne vend pas.

---

## 7. Sujets suggérés — 12 prochaines semaines

Tous ancrés cuisine / agencement / Tarbes, sans besoin de données chiffrées.

| # | Sujet | Angle |
|---|---|---|
| 1 | Îlot ou péninsule : que choisir selon la taille de votre cuisine ? | Arbitrage d'implantation, dégagements de circulation |
| 2 | Cuisine ouverte sur le séjour : les points à anticiper | Acoustique, hotte, continuité des sols, rangement |
| 3 | Quel plan de travail choisir : stratifié, composite, céramique ou bois ? | Comparatif par usage et par entretien |
| 4 | Rénover la cuisine d'une maison de ville tarbaise | Bâti ancien, murs non d'équerre, réseaux existants |
| 5 | Bien éclairer sa cuisine : plan de travail, îlot, ambiance | Lien naturel vers la page Luminaires |
| 6 | Tiroirs ou placards bas ? Le match du rangement | Ergonomie, accessibilité, aménagements intérieurs |
| 7 | Petite cuisine : 8 façons de gagner des centimètres utiles | Sur-mesure en surface contrainte |
| 8 | Comment lire et comparer un devis de cuisine, poste par poste | Méthode ; aucun montant cité |
| 9 | Dressing sur mesure : de la prise de cotes à la pose | Élargissement vers l'agencement |
| 10 | Salle de bains sur mesure en Hautes-Pyrénées : par où commencer | Second métier de l'entreprise |
| 11 | Cuisine et électroménager : les choix à figer avant le plan | Encastrement, évacuations, alimentation |
| 12 | Préparer sa visite en showroom : la check-list avant de venir | Conversion directe vers la prise de rendez-vous |

**Rythme conseillé :** un article toutes les deux semaines. Mieux vaut six
articles solides et bien maillés que douze articles superficiels.

---

## 8. Points en attente

### Résolus

- **`/mentions.html`** : page créée puis complétée avec les informations
  légales définitives de JPA (SAS, RCS Tarbes 823 043 419), la section RGPD
  et la section cookies. Plus aucun champ en attente.
- **`boutique.html`** : le `<title>` dupliqué `ADesign — Accueil` a été
  corrigé en `ADesign — Boutique`, `og:title` et `twitter:title` alignés.
- **`luminaire.html`** : l'image `kira.jpg` (inexistante) pointe désormais
  vers `kira.png`.

- **Google Ads** : la campagne n'étant plus active, le tag `AW-18225485568`
  a été entièrement retiré des 12 pages, avec les appels
  `trackDevisConversion()` des formulaires de devis. Le site ne dépose donc
  plus aucun cookie, ce qui rend le bandeau de consentement inutile.
  **Si une campagne est relancée un jour**, le tag devra être remis *et* la
  section « Cookies » des mentions légales réécrite : elle affirme
  aujourd'hui l'absence totale de cookie. Un bandeau de consentement
  deviendra alors nécessaire, les cookies publicitaires n'en étant pas
  exemptés.

### En attente d'arbitrage

1. **Adresse.** Le siège social déclaré est au **28** Cours Gambetta, alors
   que le footer et le JSON-LD `LocalBusiness` de toutes les pages indiquent
   le **26** Cours Gambetta. Si le showroom et le siège sont à deux adresses
   distinctes, tout est cohérent ; s'il s'agit d'une coquille, il faut
   corriger le numéro partout (footers, JSON-LD, fiche Google Business).

# Publication automatique du blog

Chaque **lundi à 9 h UTC**, le workflow `.github/workflows/blog-auto.yml` lance
`scripts/generate-article.py`, qui rédige un article, crée
`/blog/<slug>/index.html`, ajoute la card dans `/blog/index.html`, met à jour
`sitemap.xml` et `blog/rss.xml`, puis committe et pousse sur `main`.

Le script suit les règles de `/BLOG_WORKFLOW.md` : aucun prix, aucun chiffre
inventé, aucune norme citée, ancrage local Tarbes / Hautes-Pyrénées, ton
expert-conseil. Il **ne réécrit jamais** un article existant.

---

## 1. Ajouter la clé OPENAI_API_KEY

Sans ce secret, le workflow échoue immédiatement avec un message clair.

1. Créer une clé sur <https://platform.openai.com/api-keys>.
2. Sur GitHub, ouvrir le dépôt → **Settings** → **Secrets and variables** →
   **Actions**.
3. Bouton **New repository secret**.
4. Name : `OPENAI_API_KEY` — Secret : coller la clé.
5. **Add secret**.

La clé n'apparaît jamais dans les logs : GitHub la masque automatiquement.

---

## 2. Déclencher le workflow à la main

Onglet **Actions** → workflow **blog-auto** dans la colonne de gauche →
bouton **Run workflow** → branche `main` → **Run workflow**.

Le job dure une à deux minutes. Cliquer dessus affiche le journal complet :
sujet retenu, titre, slug, longueur de la meta description, nombre de mots,
fichiers écrits.

---

## 3. Ajouter de nouveaux sujets

Le script pioche dans le tableau du **§ 7 « Sujets suggérés »** de
`/BLOG_WORKFLOW.md`, dans l'ordre, et saute les sujets déjà publiés.

Il suffit d'ajouter des lignes au tableau, en continuant la numérotation :

```markdown
| 13 | Aménager une buanderie attenante à la cuisine | Rangement, plan de travail, évacuation |
| 14 | Bibliothèque sur mesure dans un séjour | Prise de cotes, contournement des menuiseries |
```

Trois colonnes : **numéro**, **sujet**, **angle**. L'angle est envoyé au modèle
avec le sujet, c'est lui qui oriente vraiment le contenu — le soigner.

Pour associer une image d'en-tête à un nouveau sujet, ajouter une entrée dans
`topic_images` de `/blog-config.json` (`"13": { "src": "...", "alt": "..." }`).
Sans entrée, le script utilise `default_image`. Il vérifie que l'image existe
dans le dépôt et se rabat sur `/assets/cuisine-1.jpg` sinon.

Quand tous les sujets sont publiés, le workflow sort en **succès** avec le
message « Aucun nouvel article à publier cette semaine » et ne committe rien.

---

## 4. Tester en local

```bash
pip install openai
export OPENAI_API_KEY="sk-..."

# Génère et valide TOUT sans écrire un seul fichier ni committer
python3 scripts/generate-article.py --dry-run

# Forcer un sujet précis du tableau § 7
python3 scripts/generate-article.py --dry-run --topic 5

# Récupérer le HTML complet pour l'inspecter, hors du dépôt
python3 scripts/generate-article.py --dry-run --out /tmp/apercu.html
```

Codes de sortie : `0` succès · `78` rien à faire · `1` erreur.

---

## 5. Coût

Modèle `gpt-4o-mini` (fixé dans `blog-config.json`), environ 1 500 tokens en
entrée et 3 500 en sortie par article.

| | Par article | 12 articles | 52 semaines |
|---|---|---|---|
| Coût estimé | **≈ 0,002 $** | ≈ 0,03 $ | ≈ 0,12 $ |

Autrement dit : quelques centimes par an. Les minutes GitHub Actions sont
gratuites sur un dépôt public.

Pour changer de modèle, modifier `openai_model` dans `/blog-config.json` —
aucune ligne de code à toucher.

---

## 6. En cas d'échec

Le job passe au rouge et **GitHub envoie un e-mail au propriétaire du dépôt**.
Le journal du job indique la cause exacte ; rien n'est écrit ni committé tant
que l'article n'est pas entièrement généré et validé, donc un échec ne laisse
jamais le site dans un état intermédiaire.

Causes les plus courantes :

| Message | Cause | Correction |
|---|---|---|
| `OPENAI_API_KEY est vide` | secret absent | § 1 ci-dessus |
| `l'appel OpenAI a échoué` | quota, panne, réseau | relancer à la main |
| `article trop court` | réponse dégradée du modèle | relancer à la main |
| `gabarit inattendu` | l'article de référence a changé de structure | vérifier `reference_article` dans `blog-config.json` |

---

## 7. Pagination — le seul point à surveiller

`/blog/` affiche toutes les cards tant qu'il y en a **6 ou moins**. Au-delà, le
script continue de fonctionner mais écrit un avertissement dans le journal :

```
! 7 cards sur /blog/ : il est temps de paginer (voir BLOG_WORKFLOW.md § 3).
```

La pagination reste une opération manuelle (§ 3 de `BLOG_WORKFLOW.md`) : elle
déplace des articles existants, et le script a pour consigne de ne jamais y
toucher. Le site reste parfaitement valide en attendant, la page est
simplement plus longue.

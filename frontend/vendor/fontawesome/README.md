# Correctif : restaurer vendor/fontawesome (version all.min.css)

## Le problème

Le commit "vendor" (`1e25f01`) qui a restauré `leaflet`/`markercluster`/etc a,
au passage, supprimé tout `frontend/vendor/fontawesome/` — alors que
`sw.js` et les 8 pages HTML (`index.html`, `analyse.html`, `cgu.html`,
`dashboard.html`, `droits.html`, `mentions-legales.html`,
`paysages.html`, `devenir-guide.html`) référencent toutes
`/vendor/fontawesome/css/all.min.css`. Résultat : ce fichier 404,
aucune icône FontAwesome ne s'affiche.

**Aucun fichier HTML/JS n'a besoin d'être modifié** — les références
`all.min.css` sont déjà correctes partout. Il ne manque que le dossier
lui-même.

## Le patch

Copiez tel quel le dossier `vendor/` de cette archive dans
`frontend/`, de façon à obtenir :

```
frontend/vendor/fontawesome/
├── LICENSE.txt
├── css/
│   └── all.min.css
└── webfonts/
    ├── fa-brands-400.woff2
    ├── fa-regular-400.woff2
    └── fa-solid-900.woff2
```

```bash
# depuis la racine du repo
cp -r vendor/fontawesome frontend/vendor/
git add frontend/vendor/fontawesome
git commit -m "fix: restaurer vendor/fontawesome (all.min.css) supprimé par erreur"
git push
```

## Pourquoi ces fichiers précisément

- `all.min.css` regroupe les 3 styles (solid/regular/brands) + tout le
  mapping icône → glyphe en un seul fichier, comme vos pages
  l'attendent déjà (une seule balise `<link>` par page).
- Seuls les `.woff2` sont inclus (pas de `.ttf`) : c'est déjà ce que
  la version précédente avait (confirmé par le diff du commit qui les
  a supprimés), et woff2 est supporté par tous les navigateurs
  modernes depuis des années — le `.ttf` n'est qu'un repli pour de
  très vieux navigateurs.
- Licence Font Awesome Free incluse (`LICENSE.txt`) : les icônes sont
  sous CC BY 4.0, ça vaut le coup de la garder dans le repo.

## Après déploiement

Rechargez une page en forçant le cache (Ctrl+Maj+R) et vérifiez dans
l'onglet Network des DevTools que `all.min.css` répond bien en `200`
et non plus en `404`.

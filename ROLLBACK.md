# DEPLOY & ROLLBACK : refonte DA de suivi

Refonte du front de jobhunter dans la DA de suivi (papier crème, encre chaude, accent rouge, Helvetica Neue + JetBrains Mono). Faite le 02/07/2026.

## État actuel (sûr)

- `main` = **exactement la version en prod** (Render), NON touchée. Repère : tag `pre-redesign-backup` (commit 6a2192f).
- Branche `redesign-da-suivi` = la nouvelle version, prête, committée (2 commits).
- Backup fichier de l'ancien front : `static/index.html.pre-redesign.bak`.
- Rien n'a été poussé ni déployé. La prod affiche toujours l'ancienne version.

Vérifié en local : login, dashboard, recherche réelle (35 offres France Travail), analytics, dark mode. 0 tiret cadratin, 0 ombre, 0 emoji, aucune feature cassée.

## POUR DÉPLOYER (mettre la nouvelle DA en prod)

Depuis `~/Desktop/Projets/Recherche d'emploi/jobhunter-ai/` :

```bash
git checkout main
git merge --no-ff redesign-da-suivi -m "Merge refonte DA de suivi"
git push origin main
```

Render redéploie automatiquement le service depuis `main`. Compter 1 à 3 min.
Note : le compte GitHub connecté localement (`aurelienmicheau-hub`) doit avoir les droits d'écriture sur `graniertitou-cloud/jobhunter-ai`. Sinon, se connecter avec le bon compte : `gh auth login`.

## POUR REVENIR EN ARRIÈRE (rollback)

### Cas 1 : déjà mergé/poussé, on veut annuler proprement (recommandé)
Crée un commit qui inverse la refonte, sans réécrire l'historique :
```bash
git checkout main
git revert --no-edit -m 1 <hash_du_merge>   # le hash s'affiche après le merge
git push origin main
```

### Cas 2 : revenir brutalement à l'état prod d'avant (force)
```bash
git checkout main
git reset --hard pre-redesign-backup
git push --force origin main
```

### Cas 3 : le plus rapide, sans git
Dashboard Render du service jobhunter : onglet "Deploys" > choisir le déploiement précédent > "Rollback". Instantané.

### Restaurer juste le fichier front (sans toucher au reste)
```bash
cp static/index.html.pre-redesign.bak static/index.html
```

## Nettoyage une fois la refonte validée en prod

```bash
git branch -d redesign-da-suivi          # supprime la branche locale
rm static/index.html.pre-redesign.bak    # supprime le backup fichier
git tag -d pre-redesign-backup           # supprime le repère (optionnel, à garder par prudence)
```

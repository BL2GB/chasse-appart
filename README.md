# chasse-appart

Robot qui relève les annonces de location des régies genevoises et de quelques portails.

- `sources.yaml` : régies et portails relevés, et pré-filtre large. Ajouter une régie = ajouter une ligne.
- `crawl.py` : vérifie le robots.txt, ouvre chaque page de liste dans Chromium headless, suit la pagination
  (« suivant », « voir plus »), extrait les fiches, lit la fiche détaillée des annonces plausibles.
- `data/batches/` : un fichier par passage avec uniquement les fiches nouvelles (gardé 30 jours).
- `data/run.json` : état de chaque source au dernier passage.

Le tri selon les critères (réglés dans le tableau de bord) est fait ensuite par une tâche planifiée Claude.

**Phase de test** : la planification est désactivée. Lancer à la main : onglet **Actions** > **crawl** >
**Run workflow** (champ facultatif : ids de sources à tester, séparés par des espaces).

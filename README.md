# StreamLocal

StreamLocal est une application Windows locale pour rechercher et lire des
films, séries, animes et mangas/scans depuis les providers intégrés. L'app se
lance dans une fenêtre native, garde les données sur ton PC, et peut créer son
propre fichier `.exe` à partir du code téléchargé.

Le dépôt public est pensé pour les utilisateurs. Il contient le script de
création de l'exécutable, mais pas les scripts de maintenance utilisés pour
pousser les mises à jour.

Version actuelle : `0.6.6` - V6 finalisée le 10/05/2026, avec Scan-Manga.

## Installation Rapide

1. Télécharge le dépôt depuis GitHub avec `Code` puis `Download ZIP`.
2. Décompresse le fichier ZIP.
3. Ouvre le dossier extrait, par exemple `StreamLocal-main`.
4. Double-clique sur `creer_exe.bat`.
5. À la fin, lance `dist\AutoFlix.exe`.

## Prérequis

Il faut Python 3.12 pour créer l'exécutable.

Le script vérifie automatiquement Python. Si Python 3.12 est absent, il affiche
le lien officiel :

```text
https://www.python.org/downloads/windows/
```

Pendant l'installation de Python, coche `Add python.exe to PATH`.

## Ce Que Fait `creer_exe.bat`

Le fichier `creer_exe.bat` automatise toute la création de l'application :

1. Il vérifie que le dossier contient bien le projet.
2. Il cherche Python 3.12 avec `py -3.12`, puis avec `python`.
3. Il refuse d'écraser `dist\AutoFlix.exe` si l'application est déjà ouverte.
4. Il crée un environnement local `.venv` si nécessaire.
5. Il active ou met à jour `pip`.
6. Il vérifie si les dépendances sont déjà installées.
7. Si elles manquent, il installe le projet, ses dépendances et PyInstaller.
8. Il compile rapidement les fichiers Python importants pour détecter les erreurs.
9. Il lance PyInstaller avec `build\autoflix.spec`.
10. Il crée l'exécutable final dans `dist\AutoFlix.exe`.

Si tu relances le script plus tard, il réutilise ce qui existe déjà quand c'est
possible. Il ne réinstalle pas tout inutilement si l'environnement `.venv` est
déjà correct.

## Où Sont Créés Les Fichiers

Après la création, tu trouveras :

```text
dist\AutoFlix.exe
```

Le script crée aussi des dossiers techniques locaux :

```text
.venv\
build\autoflix\
dist\
```

Ces dossiers sont normaux. Ils servent à construire l'application et ne doivent
pas être envoyés sur GitHub.

Les vidéos téléchargées depuis l'application sont enregistrées dans un dossier
local à côté du projet :

```text
StreamLocal-main\Téléchargements\
```

Si tu as renommé le dossier extrait, le dossier `Téléchargements` est créé dans
ce dossier renommé.

## Lancer L'Application

Double-clique sur :

```text
dist\AutoFlix.exe
```

L'application ouvre une fenêtre AutoFlix. Elle peut aussi rester dans la zone de
notification Windows si l'option de réduction dans le tray est activée.

## Données Locales

StreamLocal garde les données utilisateur sur ton ordinateur :

- historique de lecture;
- progression;
- favoris;
- réglages;
- téléchargements;
- suivi local des épisodes et chapitres;
- notifications en attente.

Ces données ne sont pas incluses dans le dépôt GitHub.

## Téléchargements Vidéo

Les téléchargements depuis l'interface utilisent FFmpeg. Si FFmpeg n'est pas
disponible dans le `PATH`, l'application peut lire les contenus mais les
téléchargements peuvent être indisponibles.

FFmpeg peut être installé depuis :

```text
https://ffmpeg.org/download.html
```

Par défaut, les fichiers vont dans `Téléchargements\` à la racine du dossier
StreamLocal. Dans l'onglet `Téléchargements`, tu peux voir le dossier courant,
filtrer la liste (`Tous`, `Actifs`, `Terminés`, `Erreurs`) et utiliser les
actions `Lire`, `Ouvrir le dossier`, `Réessayer`, `Annuler` ou `Supprimer`.

`Supprimer` efface le fichier vidéo du disque et retire l'entrée de la liste.
Un téléchargement actif doit d'abord être annulé. Le bouton `Supprimer terminés`
efface aussi les fichiers des téléchargements terminés, annulés ou en erreur.

Depuis la V6, les téléchargements HLS sont plus tolérants avec les sources
fragiles : Anime-Sama, GoldenMS/VidEasy et GoldenAnime/AllAnime ont été
renforcés, et les fichiers téléchargés depuis plusieurs lecteurs gardent le nom
du lecteur dans le fichier pour éviter les collisions.

## Nouveautés V6

- Téléchargements HLS corrigés pour les playlists et segments obfusqués.
- GoldenAnime/AllAnime charge les sources plus vite et résout les liens chiffrés.
- GoldenMS/VidEasy peut télécharger les flux qui restaient bloqués à 0%.
- Scan-Manga est disponible dans le lecteur `Manga`, avec chargement des pages
  via le proxy local quand les images refusent le chargement direct.
- Les téléchargements manuels depuis plusieurs lecteurs ne s'écrasent plus.
- Le double-clic sur la vidéo bascule correctement le plein écran une seule fois.

## Manga Et Scans

L'onglet `Manga` utilise le lecteur scans unique de l'application. Il regroupe
les providers manga/scans disponibles, notamment Anime-Sama, Lelscans et
Scan-Manga quand ils répondent.

Certains chapitres Scan-Manga peuvent être temporairement indisponibles si le
site impose une vérification Cloudflare ou si le chapitre est payant. Dans ce
cas, l'application affiche une erreur lisible au lieu de bloquer le lecteur.

En plein écran, le lecteur scans permet :

- zoom à la molette;
- déplacement par clic maintenu;
- changement de page par clic gauche/droite en mode page.

## Problèmes Courants

### Python 3.12 est introuvable

Installe Python 3.12 depuis :

```text
https://www.python.org/downloads/windows/
```

Puis relance `creer_exe.bat`.

### `dist\AutoFlix.exe` est verrouillé

AutoFlix est probablement déjà ouvert. Ferme l'application depuis la fenêtre ou
depuis la zone de notification Windows, puis relance `creer_exe.bat`.

### Le premier build est long

C'est normal. Le premier lancement télécharge les dépendances Python et construit
l'exécutable. Les lancements suivants réutilisent l'environnement local quand il
est déjà prêt.

### Des warnings apparaissent pendant PyInstaller

Certains warnings de dépendances sont normaux pendant le build. Le point
important est la ligne finale indiquant que `dist\AutoFlix.exe` a été créé.

## Ce Qui Est Inclus Dans Le Dépôt

Le dépôt contient :

- le code source de l'application;
- les fichiers de configuration publics dans `data\`;
- les fichiers de build dans `build\`;
- `creer_exe.bat` pour construire l'exécutable;
- les tests et fichiers nécessaires au projet.

Le dépôt ne doit pas contenir :

- `.venv\`;
- `dist\`;
- `build\autoflix\`;
- `Téléchargements\`;
- fichiers de logs;
- caches Python;
- données utilisateur locales;
- scripts de push ou maintenance privée.

## Licence Et Responsabilité

StreamLocal est un outil local qui automatise la recherche et la lecture via des
providers tiers. Il n'héberge aucun contenu. Utilise-le uniquement là où tu as le
droit d'accéder aux contenus concernés, et respecte les lois et conditions qui
s'appliquent dans ton pays.

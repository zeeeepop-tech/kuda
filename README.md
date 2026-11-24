# kuda.

Culture events in Banja Luka (theatre, concerts, talks), collected every morning from the venues' own websites.

- `collect.py` reads Narodno pozorište RS, Banski dvor and SC Borik, keeps culture only and writes `data/events.json` and `data/events.js`.
- `index.html` is the site. It reads `data/events.js`.
- `.github/workflows/daily.yml` runs the collector every morning on GitHub and publishes the site.

Run it on your own computer: `python3 collect.py`, then open `index.html`.

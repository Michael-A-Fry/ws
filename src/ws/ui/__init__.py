"""The shared page toolkit: the ws look as CSS, small HTML components, and the loopback server that serves a page.

`ws setup --web` and the ws home page both read this package, so they look and behave alike. `css` is the type, space
and radius scale and the base rules, built from the `ws.brand` tokens (dark and light). `components` are pure
functions that return escaped HTML (`panel`, `row`, `status`, `mark`, `sparkline`, `page`, and for the home page `empty`, `chip`, `stat`, `link`,
`action`). `server` is the loopback
server: a random token, Host and Origin checks, a nonce Content-Security-Policy and an idle shutdown, over a table of
GET and POST routes. Stdlib only; no CDN and no external fonts.
"""

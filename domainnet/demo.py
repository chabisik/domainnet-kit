"""A ready-made scenario used by ``python -m domainnet``."""

DEMO_SCENARIO = """\
# t(s)  command
0    add alpha bravo charlie
0    deploy alpha echo
0    deploy bravo counter
0    deploy charlie upper
0.4  link alpha bravo 140
0.8  link bravo charlie 200
2    traffic on 2

6    add delta
6.2  link delta alpha 120
6.4  link delta charlie 260
6.6  deploy delta clock
6.8  deploy delta gateway

11   add echo-dom
11.2 link echo-dom bravo 100
11.4 deploy echo-dom dice
11.6 deploy echo-dom broadcast
13   send delta bravo/counter hello
14   send alpha delta/gateway {"target": "charlie/upper", "payload": "routed through two domains"}

20   unlink alpha bravo
24   link alpha bravo 90
28   rm charlie
30   add foxtrot
30.2 link foxtrot delta 150
30.4 link foxtrot echo-dom 90
30.6 deploy foxtrot ping
"""

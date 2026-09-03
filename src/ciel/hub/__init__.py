"""The hub: Ciel's brain half, served over the wire.

``ciel hub`` runs the pipeline with no microphone anywhere: the brain,
memory, Vigil, the Discord lane, the Chart, timers' bookkeeping, and
the confirm broker's choreography — everything that is judgment,
memory, or a text lane. The spoke (``ciel spoke``) is its ears and
mouth, one more client of the same socket the Chart uses. Both run on
this machine for now; the hub moves to a server later and nothing
here changes but an address.
"""

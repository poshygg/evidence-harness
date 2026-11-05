"""Alternative candidate. Its performance is measured rather than assumed."""


def unique_in_order(values):
    output = []
    seen = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output

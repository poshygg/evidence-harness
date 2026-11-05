"""Small, deliberately simple reference candidate for synthetic integer lists."""


def unique_in_order(values):
    output = []
    for value in values:
        if value not in output:
            output.append(value)
    return output

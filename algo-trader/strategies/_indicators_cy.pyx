# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True
"""Cython versions of the hot indicators in strategies/indicators.py.

Sums use the same compensated (Neumaier) accumulation as CPython 3.12's ``sum``
over floats, so results match the pure-Python functions bit for bit.
"""

from libc.math cimport fabs, sqrt


cdef inline void _accumulate(double x, double *total, double *compensation) noexcept nogil:
    cdef double t = total[0] + x
    if fabs(total[0]) >= fabs(x):
        compensation[0] += (total[0] - t) + x
    else:
        compensation[0] += (x - t) + total[0]
    total[0] = t


def rsi(object values, int period=14):
    cdef Py_ssize_t n = len(values)
    cdef Py_ssize_t i
    cdef double gains = 0.0, gains_c = 0.0, losses = 0.0, losses_c = 0.0
    cdef double change, relative_strength
    if period <= 0 or n < period + 1:
        return None
    for i in range(n - period, n):
        change = float(values[i]) - float(values[i - 1])
        _accumulate(change if change > 0.0 else 0.0, &gains, &gains_c)
        _accumulate(-change if -change > 0.0 else 0.0, &losses, &losses_c)
    gains = (gains + gains_c) / period
    losses = (losses + losses_c) / period
    if losses == 0:
        return 100.0 if gains else 50.0
    relative_strength = gains / losses
    return 100 - 100 / (1 + relative_strength)


def efficiency_ratio(object values, int period=20):
    cdef Py_ssize_t n = len(values)
    cdef Py_ssize_t index
    cdef double path = 0.0, path_c = 0.0, move
    if period <= 0 or n < period + 1:
        return None
    for index in range(n - period, n):
        move = float(values[index]) - float(values[index - 1])
        _accumulate(fabs(move), &path, &path_c)
    path = path + path_c
    if path <= 0:
        return 0.0
    return min(1.0, fabs(float(values[n - 1]) - float(values[n - 1 - period])) / path)


def population_std(object values):
    cdef Py_ssize_t n = len(values)
    cdef Py_ssize_t i
    cdef double total = 0.0, total_c = 0.0, mean, squares = 0.0, squares_c = 0.0, deviation
    if n == 0:
        return None
    for i in range(n):
        _accumulate(float(values[i]), &total, &total_c)
    mean = (total + total_c) / n
    for i in range(n):
        deviation = float(values[i]) - mean
        _accumulate(deviation * deviation, &squares, &squares_c)
    return sqrt((squares + squares_c) / n)

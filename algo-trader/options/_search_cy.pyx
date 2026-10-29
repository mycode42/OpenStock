# cython: language_level=3

"""Compiled scorer for the options research inverse-distance surrogate."""

from libc.math cimport sqrt


cpdef void prepare_surrogate_arrays(
    list candidates,
    int[:, ::1] coordinates,
    unsigned char[:] active,
    double[:] baseline_distances,
    list candidate_ids,
    const unsigned char[:, ::1] baseline_parameter_matches,
    const unsigned char[:] baseline_regime_matches,
):
    cdef Py_ssize_t candidate_count = len(candidates)
    cdef Py_ssize_t dimensions = coordinates.shape[1]
    cdef Py_ssize_t index, dimension
    cdef int regime_index, choice_index
    cdef double squared
    cdef object candidate, grid_indices

    candidate_ids.clear()
    for index in range(candidate_count):
        candidate = candidates[index]
        grid_indices = candidate.grid_indices
        candidate_ids.append(candidate.candidate_id)
        if grid_indices:
            for dimension in range(min(dimensions, len(grid_indices))):
                coordinates[index, dimension] = grid_indices[dimension]
        if candidate.is_baseline:
            active[index] = 0
            continue
        if not grid_indices:
            # Pinned champions have no grid position; the caller sets their distance.
            continue

        regime_index = grid_indices[0]
        squared = 0.0
        for dimension in range(1, dimensions):
            choice_index = grid_indices[dimension]
            if baseline_parameter_matches[dimension - 1, choice_index] == 0:
                squared += 1.0
        if baseline_regime_matches[regime_index] == 0:
            squared += 1.0
        baseline_distances[index] = sqrt(squared)


cpdef int best_surrogate_index(
    const int[:, ::1] coordinates,
    const unsigned char[:] active,
    const double[:] baseline_distances,
    const int[:, ::1] known_coordinates,
    const unsigned char[:] known_has_grid,
    const double[:] known_scores,
    const double[:] scales,
    list candidate_ids,
    double exploration,
):
    cdef Py_ssize_t candidate_count = coordinates.shape[0]
    cdef Py_ssize_t dimensions = coordinates.shape[1]
    cdef Py_ssize_t known_count = known_scores.shape[0]
    cdef Py_ssize_t index, known_index, dimension
    cdef int best_index = -1
    cdef double best_score = -1.0e300
    cdef double numerator, denominator, novelty, distance
    cdef double squared_distance, delta, weight, prediction, selection_score

    for index in range(candidate_count):
        if active[index] == 0:
            continue
        numerator = 0.0
        denominator = 0.0
        novelty = 1.0e300
        for known_index in range(known_count):
            if known_has_grid[known_index] == 0:
                distance = baseline_distances[index]
            else:
                squared_distance = 0.0
                if coordinates[index, 0] != <int>known_coordinates[known_index, 0]:
                    squared_distance = 1.0
                for dimension in range(1, dimensions):
                    delta = (
                        coordinates[index, dimension]
                        - known_coordinates[known_index, dimension]
                    ) / scales[dimension - 1]
                    squared_distance += delta * delta
                distance = sqrt(squared_distance)
            if distance < 1.0e-6:
                distance = 1.0e-6
            weight = 1.0 / (distance * distance)
            numerator += weight * known_scores[known_index]
            denominator += weight
            if distance < novelty:
                novelty = distance
        prediction = numerator / denominator
        selection_score = prediction + exploration * novelty
        if selection_score > best_score:
            best_score = selection_score
            best_index = <int>index
        elif selection_score == best_score and best_index >= 0:
            if candidate_ids[index] > candidate_ids[best_index]:
                best_index = <int>index
    return best_index

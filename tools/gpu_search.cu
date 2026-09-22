// One independent hill climber per CUDA thread. All scores use integers.
// ponytail: fully rescore each trial; use incremental scoring if larger batches need it.
__device__ unsigned next_random(unsigned &state) {
    state ^= state << 13; state ^= state >> 17; state ^= state << 5;
    return state;
}
__device__ long long score_layout(
    const int *pos, const int *rot, int n, int a, int t, int level_scale,
    const int *valid, const int *effects, const int *multipliers,
    const int *disabled, const int *base, const int *caps,
    const int *weights, const int *doubles
) {
    for (int j = 0; j < t; ++j)
        if (!valid[(j * n + pos[a+j]) * 4 + rot[j]]) return -1;
    long long primary = 0, secondary = 0, active = 0;
    for (int i = 0; i < a; ++i) {
        int cell = pos[i], bonus = 0, mult = doubles[cell], blocked = 0;
        for (int j = 0; j < t; ++j) {
            int idx = ((j * n + pos[a+j]) * 4 + rot[j]) * n + cell;
            bonus += effects[idx]; mult += multipliers[idx]; blocked |= disabled[idx];
        }
        int level = min(caps[i], (base[i] + bonus) * max(1, mult));
        if (level >= 0 && !blocked) {
            primary += (long long)level * weights[i]; secondary += level; ++active;
        }
    }
    return (primary * level_scale + secondary) * (a + 1) + active;
}
extern "C" __global__ void search(
    int n, int a, int t, int count, int steps, unsigned seed, int level_scale,
    const int *valid, const int *effects, const int *multipliers,
    const int *disabled, const int *base, const int *caps,
    const int *weights, const int *doubles,
    int *positions, int *rotations, long long *scores
) {
    int id = blockDim.x * blockIdx.x + threadIdx.x;
    if (id >= count) return;
    unsigned rng = seed ^ ((id + 1u) * 747796405u);
    if (!rng) rng = 1;
    int pos[60], rot[60];
    for (int i = 0; i < n; ++i) pos[i] = i;
    for (int i = n-1; i > 0; --i) {
        int j = next_random(rng) % (i+1), tmp = pos[i]; pos[i] = pos[j]; pos[j] = tmp;
    }
    for (int j = 0; j < t; ++j) rot[j] = next_random(rng) % 4;
    long long current = score_layout(pos, rot, n, a, t, level_scale,
        valid, effects, multipliers, disabled, base, caps, weights, doubles);
    for (int step = 0; step < steps; ++step) {
        bool rotate = t && next_random(rng) % 4 == 0;
        int i = next_random(rng) % (rotate ? t : n);
        int j = next_random(rng) % n, old = rotate ? rot[i] : pos[i];
        if (rotate) rot[i] = next_random(rng) % 4;
        else { pos[i] = pos[j]; pos[j] = old; }
        long long trial = score_layout(pos, rot, n, a, t, level_scale,
            valid, effects, multipliers, disabled, base, caps, weights, doubles);
        if (trial >= current) current = trial;
        else if (rotate) rot[i] = old;
        else { pos[j] = pos[i]; pos[i] = old; }
    }
    for (int i = 0; i < n; ++i) positions[id*n+i] = pos[i];
    for (int j = 0; j < t; ++j) rotations[id*t+j] = rot[j];
    scores[id] = current;
}

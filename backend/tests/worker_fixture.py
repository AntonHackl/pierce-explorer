"""GPU-free persistent worker fixture."""
import json
import sys
import time

mode = sys.argv[1]
pairs = {}
generation = 0


def status():
    return {'pairs': list(pairs.values()), 'runtimeInitializationUs': 2000,
            'memory': {'device': 'Test GPU', 'totalBytes': 8 * 2**30, 'freeBytes': 6 * 2**30, 'reservedBytes': 8 * 2**30 // 10},
            'checkpoints': []}


for line in sys.stdin:
    request = json.loads(line)
    command = request['command']
    error = None
    code = 'native_error'
    details = None
    if command == 'load':
        if mode == 'load_error': error = 'Invalid prepared dataset'
        elif not any(pair['key'] == request['key'] for pair in pairs.values()):
            generation += 1
            handle = f'pair-{generation}'
            pairs[handle] = {'key': request['key'], 'pairHandle': handle, 'datasetLoadUs': 1000,
                             'datasetGpuBytes': 2048, 'completedQueries': 0, 'lastResult': None}
    elif command == 'query':
        if mode == 'slow': time.sleep(0.6)
        if mode == 'timeout': time.sleep(30)
        if mode == 'crash': sys.exit(7)
        if mode == 'malformed':
            print('not json', flush=True)
            continue
        if mode == 'wrong_id': request['id'] = 'other'
        if mode == 'budget':
            error, code = 'Not enough GPU memory. Unload another workload.', 'memory_budget_exceeded'
            details = {'requiredBytes': 9000000000, 'availableBytes': 5000000000, 'reservedBytes': 800000000}
        else:
            pair = pairs[request['pairHandle']]
            pair['completedQueries'] += 1
            pair['lastResult'] = {'resultCount': 0 if mode == 'zero' else 5000 if pair['key'] == 'small' else 1000000,
                                  'queryExecutionUs': -1 if mode == 'negative' else 2500,
                                  'benchmarkPhaseSumUs': 2000, 'rawHitCount': 70000,
                                  'resultBufferBytes': 80008, 'hashTableBytes': 80008, 'estimatedPairCount': 5000,
                                  'phaseTimingsUs': {
                                      'estimation': -1 if mode == 'negative_phase' else 500,
                                      'trace': 1200, 'compaction': 250, 'download': 50,
                                  }}
    elif command == 'unload':
        if mode != 'bad_unload': del pairs[request['pairHandle']]
    elif command == 'shutdown':
        if mode == 'hung_shutdown': time.sleep(30)
        pairs.clear()
    elif command != 'status': error = 'Unknown command'
    if mode == 'diagnostics':
        sys.stderr.write('diagnostic message\n' * 20000)
        sys.stderr.flush()
    response = {'version': 2 if mode == 'old_worker' else 3, 'id': request['id'], 'ok': error is None, 'data': status()}
    if error: response.update(error=error, code=code, details=details)
    print(json.dumps(response), flush=True)
    if command == 'shutdown': break

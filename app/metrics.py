from prometheus_client import Counter, Histogram, Gauge, Info

REQUEST_COUNT = Counter(
    'http_requests_total',
    'Total HTTP requests',
    ['method', 'endpoint', 'status']
)

REQUEST_LATENCY = Histogram(
    'http_request_duration_seconds',
    'HTTP request latency in seconds',
    ['method', 'endpoint'],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]
)

ACTIVE_REQUESTS = Gauge(
    'http_active_requests',
    'Number of active HTTP requests being processed'
)

ERROR_COUNT = Counter(
    'http_errors_total',
    'Total HTTP errors',
    ['method', 'endpoint', 'error_type']
)

TASKS_CREATED = Counter('tasks_created_total', 'Total tasks created')
TASKS_DELETED = Counter('tasks_deleted_total', 'Total tasks deleted')

TASKS_IN_STORE = Gauge('tasks_in_store', 'Number of tasks currently in store')

APP_INFO = Info('app', 'Application information')
APP_INFO.info({'version': '1.0.0', 'name': 'task-manager'})

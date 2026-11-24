<?php
/*
 * Посредник между чат-виджетом на сайте и ИИ-ботом на VPS (проект «AI Booking demo»).
 *
 * Виджет:  POST api/chat            → сообщение в очередь, ответ 202 {"pending": id}
 *          GET  api/result?job=id   → готовый ответ бота или снова 202
 *          GET  api/config, api/bookings/recent → данные, которые прислал бот
 * Бот:     POST api/worker/pull     → забрать сообщения (ждёт до 25 с, если очередь пуста)
 *          POST api/worker/reply    → вернуть ответ
 *          POST api/worker/state    → обновить настройки и последние записи
 * Запросы бота подписаны ключом X-Relay-Token из _data/config.php.
 *
 * Бот сам приходит за сообщениями, поэтому на VPS не нужны открытые порты и HTTPS.
 */
declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');

/**
 * Очередь сообщений храним вне папки сайта (www/<домен>/ → ../../bot-relay-data):
 * перед Apache на хостинге стоит nginx, и файлы внутри сайта он может отдать
 * напрямую, мимо запрета в .htaccess. Если туда писать нельзя — запасной вариант
 * _data рядом со скриптом (тоже закрыт .htaccess).
 */
function data_dir(): string
{
    $outside = dirname(__DIR__, 3) . '/bot-relay-data';
    if (is_dir($outside) && is_writable($outside)) {
        return $outside;
    }
    if (@mkdir($outside, 0700, true)) {
        return $outside;
    }
    return __DIR__ . '/_data';
}

define('DATA_DIR', data_dir());
define('QUEUE_DIR', DATA_DIR . '/queue');
const CONFIG_FILE = __DIR__ . '/_data/config.php';
const JOB_TTL = 120;        // секунд ждём ответа бота
const MAX_MESSAGE = 4000;   // символов; точный лимит проверяет бот
const MAX_PENDING = 200;    // защита очереди от флуда

function respond(int $status, array $body): void
{
    http_response_code($status);
    echo json_encode($body, JSON_UNESCAPED_UNICODE);
    exit;
}

function json_body(): array
{
    $raw = file_get_contents('php://input', false, null, 0, 65536);
    $data = json_decode($raw === false ? '' : $raw, true);
    return is_array($data) ? $data : [];
}

function text_length(string $s): int
{
    return function_exists('mb_strlen') ? mb_strlen($s, 'UTF-8') : strlen($s);
}

function require_worker(array $config): void
{
    $token = $_SERVER['HTTP_X_RELAY_TOKEN'] ?? '';
    if (!is_string($token) || $token === '' || !hash_equals((string)$config['token'], $token)) {
        respond(403, ['detail' => 'forbidden']);
    }
}

function valid_job_id($id): bool
{
    return is_string($id) && preg_match('/^[a-f0-9]{24}$/', $id) === 1;
}

function write_atomic(string $path, array $data): void
{
    $tmp = $path . '.tmp' . bin2hex(random_bytes(4));
    file_put_contents($tmp, json_encode($data, JSON_UNESCAPED_UNICODE), LOCK_EX);
    rename($tmp, $path);
}

function cleanup_queue(): void
{
    foreach (['pending', 'processing', 'done'] as $dir) {
        foreach (glob(QUEUE_DIR . "/$dir/*") ?: [] as $file) {
            if (filemtime($file) < time() - 600) {
                @unlink($file);
            }
        }
    }
}

$config = require CONFIG_FILE;
foreach (['pending', 'processing', 'done'] as $dir) {
    if (!is_dir(QUEUE_DIR . "/$dir")) {
        mkdir(QUEUE_DIR . "/$dir", 0700, true);
    }
}

$route = $_GET['route'] ?? '';
$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';

switch ($route) {
    case 'config':
    case 'bookings/recent':
        $file = DATA_DIR . ($route === 'config' ? '/config.json' : '/recent.json');
        if (!is_file($file)) {
            respond(503, ['detail' => 'Бот ещё не подключился. Попробуйте через минуту.']);
        }
        echo file_get_contents($file);
        exit;

    case 'chat':
        if ($method !== 'POST') {
            respond(405, ['detail' => 'Нужен POST.']);
        }
        $body = json_body();
        $session = $body['session_id'] ?? '';
        $message = $body['message'] ?? '';
        if (!is_string($session) || preg_match('/^[A-Za-z0-9_-]{8,64}$/', $session) !== 1) {
            respond(422, ['detail' => 'Неверный идентификатор диалога.']);
        }
        if (!is_string($message) || trim($message) === '' || text_length($message) > MAX_MESSAGE) {
            respond(400, ['detail' => 'Пустое или слишком длинное сообщение.']);
        }
        if (count(glob(QUEUE_DIR . '/pending/*.json') ?: []) >= MAX_PENDING) {
            respond(503, ['detail' => 'Сейчас много диалогов одновременно. Повторите через минуту.']);
        }
        $id = bin2hex(random_bytes(12));
        write_atomic(QUEUE_DIR . "/pending/$id.json", [
            'id' => $id,
            'session_id' => $session,
            'message' => $message,
            'ip' => (string)($_SERVER['REMOTE_ADDR'] ?? ''),
            'created' => time(),
        ]);
        respond(202, ['pending' => $id]);

    case 'result':
        $id = $_GET['job'] ?? '';
        if (!valid_job_id($id)) {
            respond(422, ['detail' => 'Неверный номер запроса.']);
        }
        $done = QUEUE_DIR . "/done/$id.json";
        if (is_file($done)) {
            $result = json_decode((string)file_get_contents($done), true);
            @unlink($done);
            respond((int)($result['status'] ?? 200), is_array($result['body'] ?? null) ? $result['body'] : []);
        }
        foreach (['pending', 'processing'] as $dir) {
            $file = QUEUE_DIR . "/$dir/$id.json";
            if (is_file($file)) {
                if (filemtime($file) < time() - JOB_TTL) {
                    @unlink($file);
                    respond(504, ['detail' => 'Бот не ответил вовремя. Попробуйте отправить сообщение ещё раз.']);
                }
                respond(202, ['pending' => $id]);
            }
        }
        respond(404, ['detail' => 'Запрос не найден. Отправьте сообщение ещё раз.']);

    case 'worker/pull':
        require_worker($config);
        @set_time_limit(40);
        $wait = max(0, min(25, (int)($_GET['wait'] ?? 20)));
        $deadline = microtime(true) + $wait;
        do {
            $jobs = [];
            foreach (array_slice(glob(QUEUE_DIR . '/pending/*.json') ?: [], 0, 10) as $file) {
                $claimed = QUEUE_DIR . '/processing/' . basename($file);
                if (@rename($file, $claimed)) {
                    touch($claimed);
                    $job = json_decode((string)file_get_contents($claimed), true);
                    if (is_array($job)) {
                        $jobs[] = $job;
                    }
                }
            }
            if ($jobs) {
                respond(200, ['jobs' => $jobs]);
            }
            usleep(300000);
            clearstatcache();
        } while (microtime(true) < $deadline);
        if (random_int(1, 20) === 1) {
            cleanup_queue();
        }
        respond(200, ['jobs' => []]);

    case 'worker/reply':
        require_worker($config);
        $body = json_body();
        $id = $body['id'] ?? '';
        if (!valid_job_id($id)) {
            respond(422, ['detail' => 'Неверный номер запроса.']);
        }
        @unlink(QUEUE_DIR . "/processing/$id.json");
        write_atomic(QUEUE_DIR . "/done/$id.json", [
            'status' => (int)($body['status'] ?? 200),
            'body' => is_array($body['body'] ?? null) ? $body['body'] : [],
        ]);
        respond(200, ['ok' => true]);

    case 'worker/state':
        require_worker($config);
        $body = json_body();
        if (is_array($body['config'] ?? null)) {
            write_atomic(DATA_DIR . '/config.json', $body['config']);
        }
        if (is_array($body['recent'] ?? null)) {
            write_atomic(DATA_DIR . '/recent.json', $body['recent']);
        }
        respond(200, ['ok' => true]);

    default:
        respond(404, ['detail' => 'Не найдено.']);
}

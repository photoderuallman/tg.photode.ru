<?php
declare(strict_types=1);

// The browser never sees this origin: REG.RU calls it server-to-server.
const TELEGRAM_GATEWAY_ORIGIN = 'https://tg-photode.195-19-144-52.sslip.io';

function json_error(int $status, string $code, string $message): void
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode(
        ['detail' => ['code' => $code, 'message' => $message]],
        JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE
    );
    exit;
}

if (!extension_loaded('curl')) {
    json_error(500, 'relay_unavailable', 'The hosting PHP cURL extension is unavailable.');
}

$method = strtoupper($_SERVER['REQUEST_METHOD'] ?? 'GET');
if (!in_array($method, ['GET', 'HEAD', 'POST', 'OPTIONS'], true)) {
    header('Allow: GET, HEAD, POST, OPTIONS');
    json_error(405, 'method_not_allowed', 'This relay only accepts Telegram client requests.');
}
if ($method === 'OPTIONS') {
    http_response_code(204);
    header('Cache-Control: no-store');
    exit;
}

$upstreamPath = (string)($_GET['_path'] ?? '');
$decodedPath = rawurldecode($upstreamPath);
if (
    substr($upstreamPath, 0, 5) !== '/api/'
    || strpos($decodedPath, '..') !== false
    || !preg_match("#^/api/[A-Za-z0-9._~!$&'()*+,;=:@%/-]+$#", $upstreamPath)
) {
    json_error(400, 'invalid_relay_path', 'The requested API path is invalid.');
}

$query = $_GET;
unset($query['_path']);
$upstreamUrl = TELEGRAM_GATEWAY_ORIGIN . $upstreamPath;
if ($query !== []) {
    $upstreamUrl .= '?' . http_build_query($query, '', '&', PHP_QUERY_RFC3986);
}

$incomingHeaders = function_exists('getallheaders') ? getallheaders() : [];
$headersByName = [];
foreach ($incomingHeaders as $name => $value) {
    $headersByName[strtolower((string)$name)] = (string)$value;
}
if (!isset($headersByName['authorization']) && isset($_SERVER['HTTP_AUTHORIZATION'])) {
    $headersByName['authorization'] = (string)$_SERVER['HTTP_AUTHORIZATION'];
}

$requestHeaders = [
    'Accept: ' . ($headersByName['accept'] ?? 'application/json'),
    'Origin: https://photode.ru',
    'User-Agent: photode.ru-private-telegram-relay/1.0',
];
foreach (['authorization', 'range', 'if-none-match', 'if-modified-since'] as $name) {
    if (isset($headersByName[$name]) && $headersByName[$name] !== '') {
        $requestHeaders[] = $name . ': ' . $headersByName[$name];
    }
}

$curl = curl_init($upstreamUrl);
if ($curl === false) {
    json_error(500, 'relay_unavailable', 'The hosting relay could not initialize.');
}

$responseStarted = false;
$sentBytes = 0;
$safeResponseHeaders = [
    'accept-ranges',
    'cache-control',
    'content-disposition',
    'content-length',
    'content-range',
    'content-type',
    'etag',
    'last-modified',
];

curl_setopt_array($curl, [
    CURLOPT_CUSTOMREQUEST => $method,
    CURLOPT_FOLLOWLOCATION => false,
    CURLOPT_HTTPHEADER => $requestHeaders,
    CURLOPT_HTTP_VERSION => CURL_HTTP_VERSION_1_1,
    CURLOPT_CONNECTTIMEOUT => 10,
    CURLOPT_TIMEOUT => strpos($upstreamPath, '/events/next') !== false ? 30 : 240,
    CURLOPT_SSL_VERIFYPEER => true,
    CURLOPT_SSL_VERIFYHOST => 2,
    CURLOPT_HEADERFUNCTION => static function ($handle, string $line) use (&$responseStarted, $safeResponseHeaders): int {
        $trimmed = trim($line);
        if (preg_match('~^HTTP/\S+\s+(\d{3})~i', $trimmed, $match)) {
            http_response_code((int)$match[1]);
            $responseStarted = true;
            return strlen($line);
        }
        $separator = strpos($line, ':');
        if ($separator !== false) {
            $name = strtolower(trim(substr($line, 0, $separator)));
            if (in_array($name, $safeResponseHeaders, true)) {
                header(trim(substr($line, 0, $separator)) . ': ' . trim(substr($line, $separator + 1)), true);
            }
        }
        return strlen($line);
    },
    CURLOPT_WRITEFUNCTION => static function ($handle, string $chunk) use (&$sentBytes): int {
        $sentBytes += strlen($chunk);
        echo $chunk;
        flush();
        return strlen($chunk);
    },
]);

if ($method === 'POST') {
    if ($_FILES !== []) {
        $postFields = $_POST;
        foreach ($_FILES as $name => $upload) {
            if (!is_array($upload) || ($upload['error'] ?? UPLOAD_ERR_NO_FILE) !== UPLOAD_ERR_OK) {
                json_error(400, 'invalid_upload', 'The hosting relay did not receive the uploaded media.');
            }
            $postFields[$name] = new CURLFile(
                (string)$upload['tmp_name'],
                (string)($upload['type'] ?? 'application/octet-stream'),
                basename((string)($upload['name'] ?? 'upload.bin'))
            );
        }
        curl_setopt($curl, CURLOPT_POSTFIELDS, $postFields);
    } else {
        $contentType = $headersByName['content-type'] ?? 'application/octet-stream';
        $requestHeaders[] = 'Content-Type: ' . $contentType;
        curl_setopt($curl, CURLOPT_HTTPHEADER, $requestHeaders);
        curl_setopt($curl, CURLOPT_POSTFIELDS, file_get_contents('php://input'));
    }
}

header('X-Content-Type-Options: nosniff');
header('Referrer-Policy: no-referrer');
$ok = curl_exec($curl);
$curlError = curl_error($curl);
curl_close($curl);

if ($ok === false && !$responseStarted && $sentBytes === 0) {
    json_error(502, 'gateway_unreachable', 'The Telegram gateway could not be reached from the hosting server.');
}
if ($ok === false && $sentBytes === 0 && !headers_sent()) {
    json_error(502, 'gateway_unreachable', $curlError !== '' ? 'The upstream request failed.' : 'The upstream did not respond.');
}

<?php
/**
 * Dice Dungeon: admin-only REST tool to read and make exact text edits in Elementor page data.
 * Install as a WPCode "PHP Snippet", Auto Insert, Run Everywhere.
 *
 * GET  /wp-json/dd/v1/el?id=123                 returns the page's widgets (id, type, text settings)
 * POST /wp-json/dd/v1/el {"id":123,"edits":[{"find":"...","replace":"..."}]}
 *      each "find" must occur exactly once in the raw _elementor_data JSON, or nothing is saved.
 *      Or {"id":123,"set":[{"widget":"abc123","key":"title","value":"..."}]} to replace one
 *      existing text setting of one widget (must hit exactly one widget).
 *      The previous data is kept in post meta _dd_el_backup_<timestamp>; nothing is deleted.
 *      Elementor's CSS cache is cleared after saving.
 */

add_action('rest_api_init', function () {
    $allowed = function () { return current_user_can('manage_options'); };
    register_rest_route('dd/v1', '/el', [
        ['methods' => 'GET',  'permission_callback' => $allowed, 'callback' => 'dd_el_get'],
        ['methods' => 'POST', 'permission_callback' => $allowed, 'callback' => 'dd_el_post'],
    ]);
});

// GET/POST /wp-json/dd/v1/seo  {"id":123,"key":"_yoast_wpseo_metadesc","value":"..."}  (Yoast title/description only)
add_action('rest_api_init', function () {
    $allowed = function () { return current_user_can('manage_options'); };
    register_rest_route('dd/v1', '/seo', [
        ['methods' => 'GET', 'permission_callback' => $allowed, 'callback' => function (WP_REST_Request $r) {
            $id = (int) $r->get_param('id');
            return ['id' => $id, 'title' => get_post_meta($id, '_yoast_wpseo_title', true), 'metadesc' => get_post_meta($id, '_yoast_wpseo_metadesc', true)];
        }],
        ['methods' => 'POST', 'permission_callback' => $allowed, 'callback' => function (WP_REST_Request $r) {
            $p = $r->get_json_params();
            $key = (string) ($p['key'] ?? '');
            if (!in_array($key, ['_yoast_wpseo_metadesc', '_yoast_wpseo_title'], true)) return new WP_Error('key', 'Only Yoast title/metadesc', ['status' => 400]);
            $id = (int) ($p['id'] ?? 0);
            update_post_meta($id, '_dd_seo_backup_' . time() . $key, get_post_meta($id, $key, true));
            update_post_meta($id, $key, sanitize_text_field((string) ($p['value'] ?? '')));
            return ['id' => $id, 'key' => $key, 'saved' => true];
        }],
    ]);
});

function dd_el_walk($els, &$out) {
    foreach ((array) $els as $e) {
        if (($e['elType'] ?? '') === 'widget') {
            $s = $e['settings'] ?? [];
            $txt = [];
            foreach ($s as $k => $v) {
                if (is_string($v) && $v !== '' && !preg_match('/^(_|css_|motion_fx)/', $k)
                    && !preg_match('/(color|size|typography|align|width|height|padding|margin|border|position|z_index|image|icon|animation)/', $k)) {
                    $txt[$k] = $v;
                }
            }
            foreach ($s as $k => $v) {
                if (is_array($v) && isset($v['url']) && is_string($v['url'])) $txt[$k . '.url'] = $v['url'];
            }
            $out[] = ['id' => $e['id'], 'type' => $e['widgetType'] ?? '', 'text' => $txt];
        }
        if (!empty($e['elements'])) dd_el_walk($e['elements'], $out);
    }
}

function dd_el_set(&$els, $wid, $key, $val, &$done) {
    foreach ($els as &$e) {
        if (($e['id'] ?? '') === $wid) {
            if (strpos($key, '.') !== false) {            // e.g. "link.url" for button/flip-box links
                [$k1, $k2] = explode('.', $key, 2);
                if (isset($e['settings'][$k1]) && is_array($e['settings'][$k1])) { $e['settings'][$k1][$k2] = $val; $done++; }
            } elseif (isset($e['settings'][$key])) { $e['settings'][$key] = $val; $done++; }
        }
        if (!empty($e['elements'])) dd_el_set($e['elements'], $wid, $key, $val, $done);
    }
    unset($e);
}

function dd_el_get(WP_REST_Request $req) {
    $id = (int) $req->get_param('id');
    $raw = get_post_meta($id, '_elementor_data', true);
    if (!$raw) return new WP_Error('no_data', 'No Elementor data', ['status' => 404]);
    $out = [];
    dd_el_walk(json_decode($raw, true), $out);
    return ['id' => $id, 'title' => get_the_title($id), 'widgets' => $out];
}

function dd_el_post(WP_REST_Request $req) {
    $p = $req->get_json_params();
    $id = (int) ($p['id'] ?? 0);
    $raw = get_post_meta($id, '_elementor_data', true);
    if (!$raw) return new WP_Error('no_data', 'No Elementor data', ['status' => 404]);
    $new = $raw;
    foreach (($p['edits'] ?? []) as $i => $ed) {
        $f = (string) ($ed['find'] ?? '');
        $r = (string) ($ed['replace'] ?? '');
        $n = $f === '' ? 0 : substr_count($new, $f);
        if ($n !== 1) return new WP_Error('match', "Edit $i matched $n times, nothing saved", ['status' => 400]);
        $new = str_replace($f, $r, $new);
    }
    if (!empty($p['set'])) {
        $data = json_decode($new, true);
        foreach ($p['set'] as $i => $st) {
            $done = 0;
            dd_el_set($data, (string) $st['widget'], (string) $st['key'], (string) $st['value'], $done);
            if ($done !== 1) return new WP_Error('set', "Set $i hit $done widgets, nothing saved", ['status' => 400]);
        }
        $new = wp_json_encode($data);
    }
    if (json_decode($new, true) === null) return new WP_Error('json', 'Result is not valid JSON, nothing saved', ['status' => 400]);
    update_post_meta($id, '_dd_el_backup_' . time(), wp_slash($raw));
    update_post_meta($id, '_elementor_data', wp_slash($new));
    if (class_exists('\Elementor\Plugin')) \Elementor\Plugin::$instance->files_manager->clear_cache();
    clean_post_cache($id);
    return ['id' => $id, 'saved' => true, 'edits' => count($p['edits'] ?? [])];
}

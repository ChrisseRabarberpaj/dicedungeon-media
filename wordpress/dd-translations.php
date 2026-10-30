<?php
/**
 * Dice Dungeon: bulk load Finnish translations into TranslatePress.
 * Install as a WPCode "PHP Snippet", Auto Insert, Run Everywhere. Admin only.
 *
 * GET  /wp-json/dd/v1/trp?lang=fi            all strings TranslatePress has seen (original, translated, status)
 * GET  /wp-json/dd/v1/trp?lang=fi&gettext=1  same for theme/plugin (gettext) strings
 * POST /wp-json/dd/v1/trp  {"lang":"fi","items":[{"original":"...","translated":"..."}], "gettext":false}
 *      sets the translation for each original (adds the original if TranslatePress hasn't seen it yet).
 *      Status 2 = human reviewed. Never deletes anything.
 */

add_action('rest_api_init', function () {
    $allowed = function () { return current_user_can('manage_options'); };
    register_rest_route('dd/v1', '/trp', [
        ['methods' => 'GET',  'permission_callback' => $allowed, 'callback' => 'dd_trp_get'],
        ['methods' => 'POST', 'permission_callback' => $allowed, 'callback' => 'dd_trp_post'],
    ]);
});

function dd_trp_table($lang, $gettext) {
    global $wpdb;
    $settings = get_option('trp_settings', []);
    $def  = strtolower($settings['default-language'] ?? 'en_GB');
    $lang = strtolower(preg_replace('/[^A-Za-z_]/', '', $lang));
    return $gettext ? $wpdb->prefix . 'trp_gettext_' . $lang
                    : $wpdb->prefix . 'trp_dictionary_' . $def . '_' . $lang;
}

function dd_trp_get(WP_REST_Request $req) {
    global $wpdb;
    $t = dd_trp_table($req->get_param('lang') ?: 'fi', (bool) $req->get_param('gettext'));
    if ($wpdb->get_var($wpdb->prepare('SHOW TABLES LIKE %s', $t)) !== $t)
        return new WP_Error('no_table', "No table $t yet", ['status' => 404]);
    $cols = $wpdb->get_col("SHOW COLUMNS FROM $t");
    $rows = $wpdb->get_results("SELECT * FROM $t ORDER BY id", ARRAY_A);
    return ['table' => $t, 'columns' => $cols, 'count' => count($rows), 'rows' => $rows];
}

function dd_trp_post(WP_REST_Request $req) {
    global $wpdb;
    $p = $req->get_json_params();
    $gettext = !empty($p['gettext']);
    $t = dd_trp_table($p['lang'] ?? 'fi', $gettext);
    if ($wpdb->get_var($wpdb->prepare('SHOW TABLES LIKE %s', $t)) !== $t)
        return new WP_Error('no_table', "No table $t yet", ['status' => 404]);
    $cols = $wpdb->get_col("SHOW COLUMNS FROM $t");
    $orig_table = $wpdb->prefix . 'trp_original_strings';
    $has_orig_table = !$gettext && in_array('original_id', $cols, true)
        && $wpdb->get_var($wpdb->prepare('SHOW TABLES LIKE %s', $orig_table)) === $orig_table;

    $updated = 0; $inserted = 0; $skipped = [];
    foreach (($p['items'] ?? []) as $it) {
        $o  = (string) ($it['original'] ?? '');
        $tr = (string) ($it['translated'] ?? '');
        if ($o === '' || $tr === '') { $skipped[] = $o; continue; }
        $where = $gettext && isset($it['domain'])
            ? $wpdb->prepare("original = %s AND domain = %s", $o, $it['domain'])
            : $wpdb->prepare("original = %s", $o);
        $ids = $wpdb->get_col("SELECT id FROM $t WHERE $where");
        if ($ids) {
            foreach ($ids as $id) $wpdb->update($t, ['translated' => $tr, 'status' => 2], ['id' => $id]);
            $updated += count($ids);
            continue;
        }
        if ($gettext) { $skipped[] = $o; continue; }   // gettext rows only exist once WP has used them
        $row = ['original' => $o, 'translated' => $tr, 'status' => 2];
        if (in_array('block_type', $cols, true)) $row['block_type'] = 0;
        if ($has_orig_table) {
            $oid = $wpdb->get_var($wpdb->prepare("SELECT id FROM $orig_table WHERE original = %s", $o));
            if (!$oid) { $wpdb->insert($orig_table, ['original' => $o]); $oid = $wpdb->insert_id; }
            $row['original_id'] = (int) $oid;
        }
        if ($wpdb->insert($t, $row)) $inserted++; else $skipped[] = $o;
    }
    if (function_exists('do_action')) do_action('litespeed_purge_all');
    return ['table' => $t, 'updated' => $updated, 'inserted' => $inserted, 'skipped' => $skipped];
}

/* ---------------------------------------------------------------------------
 * Finnish page titles and meta descriptions (Yoast). TranslatePress free leaves
 * these in English, so map them here. Map lives in option dd_fi_seo (en => fi),
 * set with POST /wp-json/dd/v1/trp-seo {"seo": {...}}.
 * ------------------------------------------------------------------------- */
function dd_fi_seo($s) {
    global $TRP_LANGUAGE;
    if (!is_string($s) || $s === '' || ($TRP_LANGUAGE ?? '') !== 'fi') return $s;
    $map = get_option('dd_fi_seo', []);
    $key = trim(html_entity_decode($s, ENT_QUOTES, 'UTF-8'));
    return isset($map[$key]) ? $map[$key] : $s;
}
foreach (['wpseo_title', 'wpseo_metadesc', 'wpseo_opengraph_title', 'wpseo_opengraph_desc',
          'wpseo_twitter_title', 'wpseo_twitter_description', 'pre_get_document_title'] as $f) {
    add_filter($f, 'dd_fi_seo', 99);
}
add_action('rest_api_init', function () {
    register_rest_route('dd/v1', '/trp-seo', [
        'methods' => 'POST',
        'permission_callback' => function () { return current_user_can('manage_options'); },
        'callback' => function (WP_REST_Request $req) {
            $in = $req->get_json_params()['seo'] ?? [];
            $map = get_option('dd_fi_seo', []);
            foreach ($in as $en => $fi) $map[trim(wp_strip_all_tags($en))] = wp_strip_all_tags($fi);
            update_option('dd_fi_seo', $map, false);
            do_action('litespeed_purge_all');
            return ['ok' => true, 'count' => count($map)];
        },
    ]);
});

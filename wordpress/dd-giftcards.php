<?php
/**
 * Dice Dungeon gift cards: lets the GitHub gift card job create one Amelia coupon per sold card.
 * Install as a WPCode "PHP Snippet", Auto Insert, Run Everywhere.
 *
 * POST /wp-json/dd/v1/gift-coupon   {"code":"GIFT-7K3QX2","service_id":1,"expires":"2027-10-30"}
 * GET  /wp-json/dd/v1/gift-coupon?code=GIFT-7K3QX2   (is it used?)
 * GET  /wp-json/dd/v1/gift-sale?session=cs_...     (has this Stripe payment been handled?)
 * POST /wp-json/dd/v1/gift-sale  {...}              (mark it handled, keeps the sales log here, not on GitHub)
 *
 * Only an administrator can call it (the job logs in with the "github-giftcards" application password).
 * It can only make 100 % single-use coupons, only for these four services, only with GIFT-/LAHJA- codes.
 * It never edits or deletes existing coupons or bookings.
 */

add_action('rest_api_init', function () {
    $allowed = function () { return current_user_can('manage_options'); };

    register_rest_route('dd/v1', '/gift-coupon', [
        [
            'methods'             => 'POST',
            'permission_callback' => $allowed,
            'callback'            => 'dd_gift_coupon_create',
        ],
        [
            'methods'             => 'GET',
            'permission_callback' => $allowed,
            'callback'            => 'dd_gift_coupon_get',
        ],
    ]);

    register_rest_route('dd/v1', '/gift-sale', [
        [
            'methods'             => 'GET',
            'permission_callback' => $allowed,
            'callback'            => function (WP_REST_Request $req) {
                $sales = get_option('dd_gift_sales', []);
                $sid = sanitize_text_field($req->get_param('session'));
                if ($sid === '') return array_values($sales);   // whole log
                if (!isset($sales[$sid])) return new WP_Error('not_found', 'Not handled yet', ['status' => 404]);
                return $sales[$sid];
            },
        ],
        [
            'methods'             => 'POST',
            'permission_callback' => $allowed,
            'callback'            => function (WP_REST_Request $req) {
                $p = $req->get_json_params();
                $sid = sanitize_text_field($p['session'] ?? '');
                if (!preg_match('/^cs_(live|test)_[A-Za-z0-9]+$/', $sid))
                    return new WP_Error('bad_session', 'Bad session id', ['status' => 400]);
                $keep = ['session','code','product','delivery','amount','buyer_name','buyer_email',
                         'recipient','message','sender','shipping','paid_at','handled_at'];
                $rec = [];
                foreach ($keep as $k) if (isset($p[$k])) $rec[$k] = sanitize_textarea_field((string) $p[$k]);
                $sales = get_option('dd_gift_sales', []);
                $sales[$sid] = $rec;
                update_option('dd_gift_sales', $sales, false);
                return ['ok' => true, 'count' => count($sales)];
            },
        ],
    ]);
});

function dd_gift_coupon_get(WP_REST_Request $req) {
    global $wpdb;
    $code = strtoupper(sanitize_text_field($req->get_param('code')));
    $t = $wpdb->prefix . 'amelia_coupons';
    $row = $wpdb->get_row($wpdb->prepare("SELECT * FROM $t WHERE code = %s", $code), ARRAY_A);
    if (!$row) return new WP_Error('not_found', 'No such code', ['status' => 404]);
    $used = (int) $wpdb->get_var($wpdb->prepare(
        "SELECT COUNT(*) FROM {$wpdb->prefix}amelia_customer_bookings WHERE couponId = %d AND status NOT IN ('canceled','rejected')",
        $row['id']
    ));
    return ['id' => (int) $row['id'], 'code' => $row['code'], 'status' => $row['status'],
            'expires' => $row['expirationDate'], 'used' => $used];
}

function dd_gift_coupon_create(WP_REST_Request $req) {
    global $wpdb;
    $code    = strtoupper(sanitize_text_field($req->get_param('code')));
    $service = (int) $req->get_param('service_id');
    $expires = sanitize_text_field($req->get_param('expires'));

    if (!preg_match('/^(LAHJA|GIFT)-[A-Z0-9]{4,10}$/', $code))
        return new WP_Error('bad_code', 'Code must look like GIFT-XXXXXX or LAHJA-XXXXXX', ['status' => 400]);
    // 1 = Dice Dungeon 6 hours, 7 = 12 hours, 2 = Half Shot (3 Hours), 3 = One Shot (5 Hours)
    if (!in_array($service, [1, 7, 2, 3], true))
        return new WP_Error('bad_service', 'Service not allowed', ['status' => 400]);
    if (!preg_match('/^\d{4}-\d{2}-\d{2}$/', $expires))
        return new WP_Error('bad_date', 'expires must be YYYY-MM-DD', ['status' => 400]);

    $t  = $wpdb->prefix . 'amelia_coupons';
    $ts = $wpdb->prefix . 'amelia_coupons_to_services';

    // Same code again (job retried) = return the existing coupon, never a second one.
    $existing = $wpdb->get_var($wpdb->prepare("SELECT id FROM $t WHERE code = %s", $code));
    if ($existing) return ['id' => (int) $existing, 'code' => $code, 'created' => false];

    // Fill only columns this Amelia version has.
    $cols = $wpdb->get_col("SHOW COLUMNS FROM $t");
    $want = [
        'code'                  => $code,
        'discount'              => 100,
        'deduction'             => 0,
        'limit'                 => 1,
        'customerLimit'         => 1,
        'status'                => 'visible',
        'notificationInterval'  => 0,
        'notificationRecurring' => 0,
        'expirationDate'        => $expires . ' 23:59:59',
        'allServices'           => 0,
        'allEvents'             => 0,
        'allPackages'           => 0,
    ];
    $row = array_intersect_key($want, array_flip($cols));

    $wpdb->query('START TRANSACTION');
    if (false === $wpdb->insert($t, $row)) {
        $wpdb->query('ROLLBACK');
        return new WP_Error('db', 'Coupon insert failed: ' . $wpdb->last_error, ['status' => 500]);
    }
    $id = (int) $wpdb->insert_id;
    if (false === $wpdb->insert($ts, ['couponId' => $id, 'serviceId' => $service])) {
        $wpdb->query('ROLLBACK');
        return new WP_Error('db', 'Service link failed: ' . $wpdb->last_error, ['status' => 500]);
    }
    $wpdb->query('COMMIT');
    return ['id' => $id, 'code' => $code, 'service_id' => $service, 'expires' => $expires, 'created' => true];
}

/* ---------------------------------------------------------------------------
 * Instant delivery: Stripe calls /wp-json/dd/v1/stripe-hook when a checkout completes,
 * we check Stripe's signature and start the GitHub gift card job right away.
 * The 15-minute schedule stays as a backup.
 * Secrets are pasted in Tools > Gift cards and stored in wp_options, never shown again.
 * ------------------------------------------------------------------------- */
add_action('rest_api_init', function () {
    register_rest_route('dd/v1', '/stripe-hook', [
        'methods'             => 'POST',
        'permission_callback' => '__return_true',   // Stripe signature is the auth
        'callback'            => 'dd_gift_stripe_hook',
    ]);
});

function dd_gift_stripe_hook(WP_REST_Request $req) {
    $secret  = get_option('dd_gift_stripe_whsec', '');
    $payload = $req->get_body();
    $sig     = $req->get_header('stripe-signature');
    if (!$secret || !$sig) return new WP_Error('no', 'Not configured', ['status' => 400]);

    $t = null; $v1 = [];
    foreach (explode(',', $sig) as $part) {
        [$k, $v] = array_pad(explode('=', trim($part), 2), 2, '');
        if ($k === 't') $t = $v;
        if ($k === 'v1') $v1[] = $v;
    }
    $expected = hash_hmac('sha256', $t . '.' . $payload, $secret);
    $ok = $t && abs(time() - (int) $t) < 600;
    if ($ok) { $ok = false; foreach ($v1 as $s) if (hash_equals($expected, $s)) $ok = true; }
    if (!$ok) return new WP_Error('sig', 'Bad signature', ['status' => 400]);

    $event = json_decode($payload, true);
    $type  = $event['type'] ?? '';
    if ($type !== 'checkout.session.completed' && $type !== 'checkout.session.async_payment_succeeded') {
        return ['ignored' => $type];
    }

    $token = get_option('dd_gift_github_token', '');
    $res = wp_remote_post(
        'https://api.github.com/repos/ChrisseRabarberpaj/dicedungeon-media/actions/workflows/giftcards.yml/dispatches',
        [
            'timeout' => 15,
            'headers' => [
                'Authorization'        => 'Bearer ' . $token,
                'Accept'               => 'application/vnd.github+json',
                'X-GitHub-Api-Version' => '2022-11-28',
                'User-Agent'           => 'dicedungeon-giftcards',
                'Content-Type'         => 'application/json',
            ],
            'body' => wp_json_encode(['ref' => 'main', 'inputs' => ['action' => 'run']]),
        ]
    );
    $code = is_wp_error($res) ? $res->get_error_message() : wp_remote_retrieve_response_code($res);
    update_option('dd_gift_last_hook', current_time('mysql') . ' ' . $type . ' -> GitHub ' . $code, false);
    return ['started' => $code === 204, 'github' => $code];
}

add_action('admin_menu', function () {
    add_management_page('Gift cards', 'Gift cards', 'manage_options', 'dd-gift-cards', 'dd_gift_settings_page');
});

function dd_gift_settings_page() {
    if (!current_user_can('manage_options')) return;
    if (isset($_POST['dd_gift_save']) && check_admin_referer('dd_gift_settings')) {
        foreach (['dd_gift_github_token', 'dd_gift_stripe_whsec'] as $k) {
            $v = trim(preg_replace('/\s+/', '', wp_unslash($_POST[$k] ?? '')));
            if ($v !== '') update_option($k, $v, false);
        }
        echo '<div class="notice notice-success"><p>Saved.</p></div>';
    }
    $has = fn($k) => get_option($k, '') ? '<span style="color:#1a7f37">saved ✓</span>' : '<span style="color:#b32d2e">missing</span>';
    $sales = array_reverse(array_values(get_option('dd_gift_sales', [])));
    ?>
    <div class="wrap">
      <h1>Gift cards</h1>
      <form method="post" style="max-width:640px">
        <?php wp_nonce_field('dd_gift_settings'); ?>
        <p><label><b>GitHub token</b> (<?php echo $has('dd_gift_github_token'); ?>)<br>
          <input type="password" name="dd_gift_github_token" autocomplete="off" style="width:100%" placeholder="github_pat_... (leave empty to keep)"></label></p>
        <p><label><b>Stripe webhook signing secret</b> (<?php echo $has('dd_gift_stripe_whsec'); ?>)<br>
          <input type="password" name="dd_gift_stripe_whsec" autocomplete="off" style="width:100%" placeholder="whsec_... (leave empty to keep)"></label></p>
        <p><button class="button button-primary" name="dd_gift_save" value="1">Save</button></p>
        <p>Webhook URL for Stripe: <code><?php echo esc_html(rest_url('dd/v1/stripe-hook')); ?></code><br>
          Last webhook: <?php echo esc_html(get_option('dd_gift_last_hook', 'none yet')); ?></p>
      </form>
      <h2>Sales (<?php echo count($sales); ?>)</h2>
      <table class="widefat striped"><thead><tr><th>Paid</th><th>Card</th><th>Delivery</th><th>Code</th><th>Buyer</th><th>For</th><th>Address</th></tr></thead><tbody>
      <?php foreach ($sales as $s): ?>
        <tr><td><?php echo esc_html(substr($s['paid_at'] ?? '', 0, 16)); ?></td><td><?php echo esc_html($s['product'] ?? ''); ?></td>
        <td><?php echo esc_html($s['delivery'] ?? ''); ?></td><td><code><?php echo esc_html($s['code'] ?? ''); ?></code></td>
        <td><?php echo esc_html(($s['buyer_name'] ?? '') . ' ' . ($s['buyer_email'] ?? '')); ?></td><td><?php echo esc_html($s['recipient'] ?? ''); ?></td>
        <td style="white-space:pre-line"><?php echo esc_html($s['shipping'] ?? ''); ?></td></tr>
      <?php endforeach; ?>
      </tbody></table>
    </div>
    <?php
}

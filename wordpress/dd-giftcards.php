<?php
/**
 * Dice Dungeon gift cards: lets the GitHub gift card job create one Amelia coupon per sold card.
 * Install as a WPCode "PHP Snippet", Auto Insert, Run Everywhere.
 *
 * POST /wp-json/dd/v1/gift-coupon   {"code":"LAHJA-7K3QX2","service_id":1,"expires":"2027-10-30"}
 * GET  /wp-json/dd/v1/gift-coupon?code=LAHJA-7K3QX2   (is it used?)
 * GET  /wp-json/dd/v1/gift-sale?session=cs_...     (has this Stripe payment been handled?)
 * POST /wp-json/dd/v1/gift-sale  {...}              (mark it handled, keeps the sales log here, not on GitHub)
 *
 * Only an administrator can call it (the job logs in with the "github-giftcards" application password).
 * It can only make 100 % single-use coupons, only for these four services, only with LAHJA- codes.
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

    if (!preg_match('/^LAHJA-[A-Z0-9]{4,10}$/', $code))
        return new WP_Error('bad_code', 'Code must look like LAHJA-XXXXXX', ['status' => 400]);
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

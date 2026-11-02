<?php
/**
 * Dice Dungeon: copy Role Peddlers' booking emails to Christoffer.
 * Installed 2 Nov 2026 as WPCode PHP snippet id 3353 ("DD: Bcc Christoffer on Role Peddlers booking emails"), Run Everywhere, active.
 * Amelia emails the employee a booking is assigned to. GM sessions (services 2-5) are assigned only to Role Peddlers
 * (provider 2); Christoffer (provider 3) has only the room services 1 and 7, hours 09:00-22:00.
 * This adds talecraftmanagement@gmail.com as Bcc on every email the site sends to rolepeddlers@gmail.com.
 */
add_filter('wp_mail', function ($args) {
    $to = is_array($args['to']) ? implode(',', $args['to']) : (string) $args['to'];
    if (stripos($to, 'rolepeddlers@gmail.com') === false) {
        return $args;
    }
    $headers = $args['headers'];
    if (!is_array($headers)) {
        $headers = $headers ? explode("\n", str_replace("\r\n", "\n", $headers)) : [];
    }
    $headers[] = 'Bcc: talecraftmanagement@gmail.com';
    $args['headers'] = $headers;
    return $args;
});

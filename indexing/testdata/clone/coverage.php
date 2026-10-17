<html><body><?= $title ?></body></html>
<?php
declare(strict_types=1);
namespace Demo { interface Shape { const SIDES = 0; public function area(): float; }
trait Loud { public function shout(): string { return strtoupper($this->name); } }
enum Suit: string { case Hearts = 'H'; case Spades = 'S'; }
abstract class Base implements Shape { use Loud; abstract protected function name(): string; public static int $count = 0; }
final class Impl extends Base { public function __construct(public readonly int|float $n = 1, ?string $s = null) {} public function area(): float { return $this->n ** 2; } protected function name(): string { return static::class; } }
function gen(): \Generator { yield 1; yield from [2, 3]; }
$f = fn(int $x): int => $x * 2; $g = function () use ($f) { return $f(1); };
$s = <<<EOT
heredoc {$f(2)} text
EOT;
$n = <<<'EOT'
nowdoc text
EOT;
$a = [1, 'k' => 2.5, ...[3]]; [$x, [$y]] = [1, [2]]; list('a' => $z) = ['a' => 1];
$r = match(true) { $x > 1 => 'big', default => 'small' };
try { throw new \RuntimeException("x"); } catch (\RuntimeException | \LogicException $e) { echo $e?->getMessage(); } finally { }
if ($x): echo 1; elseif ($y): echo 2; else: echo 3; endif;
while (false) { break; } do { continue; } while (false); switch ($x) { case 1: break; default: }
goto end; end: static $cache = null; global $gg; print "p"; echo "a", 'b'; $c = clone $obj; $v = $obj instanceof Impl;
#[Attribute] class Attr {}
$h = (int) "5"; $i = @$arr['x']; $j = &$k; $l = new class { public $p; }; unset($l); isset($a) && empty($b);
}

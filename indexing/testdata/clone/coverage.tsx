import React from "react";
export const View = ({ items }: { items: string[] }) => (
  <ul className="list" data-count={items.length}>
    {items.map((item, i) => <li key={i}>{item} &amp; more</li>)}
    <>text <b>bold</b></>
    <Child {...props} flag />
  </ul>
);
type TplA = `abc`; type TplB = `a${string}b`;

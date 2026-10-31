(() => {
  'use strict';
  const products = [
    { id: 'croissant', name: 'The butter half', price: 32 },
    { id: 'bun', name: 'Roll with it', price: 38 },
    { id: 'cookie', name: 'One smart cookie', price: 26 }
  ];
  const counts = new Map(products.map(product => [product.id, 0]));
  const items = document.querySelector('#box-items');
  const status = document.querySelector('#box-status');
  const dialog = document.querySelector('#box-dialog');
  const previewButton = document.querySelector('#preview-box');
  let lastAddButton;
  const money = value => `R${value}`;
  const total = () => products.reduce((sum, product) => sum + product.price * counts.get(product.id), 0);
  const count = () => [...counts.values()].reduce((sum, value) => sum + value, 0);
  function change(id, delta) {
    const product = products.find(item => item.id === id);
    if (!product) return;
    const next = counts.get(id) + delta;
    if (next < 0 || next > 12) {
      status.textContent = 'You can add up to 12 of each treat to this demo box.';
      return;
    }
    counts.set(id, next);
    render();
    status.textContent = `${product.name}: ${next} in your box. ${count()} treats in total. ${money(total())}.`;
  }
  function quantityButton(product, delta) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = delta > 0 ? '+' : '−';
    button.dataset.product = product.id;
    button.dataset.delta = delta;
    button.setAttribute('aria-label', `${delta > 0 ? 'Add one' : 'Remove one'} ${product.name}`);
    button.disabled = delta > 0 && counts.get(product.id) >= 12;
    return button;
  }
  function render() {
    const focused = document.activeElement;
    const restore = focused?.dataset.product ? { id: focused.dataset.product, delta: focused.dataset.delta } : null;
    items.replaceChildren();
    for (const product of products) {
      const quantity = counts.get(product.id);
      if (!quantity) continue;
      const row = document.createElement('li');
      row.className = 'box-item';
      const label = document.createElement('span');
      label.className = 'item-name';
      label.textContent = product.name;
      const price = document.createElement('span');
      price.className = 'item-price';
      price.textContent = `${money(product.price)} each · ${money(product.price * quantity)}`;
      label.append(price);
      const controls = document.createElement('div');
      controls.className = 'quantity';
      const amount = document.createElement('span');
      amount.textContent = quantity;
      controls.append(quantityButton(product, -1), amount, quantityButton(product, 1));
      row.append(label, controls);
      items.append(row);
    }
    document.querySelector('#box-empty').hidden = count() > 0;
    document.querySelector('#box-count').textContent = `${count()} ${count() === 1 ? 'treat' : 'treats'}`;
    document.querySelector('#box-total').textContent = money(total());
    previewButton.disabled = !count();
    document.querySelectorAll('[data-add]').forEach(button => {
      const quantity = counts.get(button.dataset.add);
      button.disabled = quantity >= 12;
      button.setAttribute('aria-label', `Add ${products.find(product => product.id === button.dataset.add).name} to your box${quantity ? `, ${quantity} already added` : ''}`);
    });
    if (restore) {
      const replacement = items.querySelector(`[data-product="${restore.id}"][data-delta="${restore.delta}"]:not(:disabled)`)
        || items.querySelector(`[data-product="${restore.id}"]:not(:disabled)`)
        || items.querySelector('button:not(:disabled)') || lastAddButton;
      replacement?.focus({ preventScroll: true });
    }
  }
  document.querySelectorAll('[data-add]').forEach(button => button.addEventListener('click', () => {
    lastAddButton = button;
    change(button.dataset.add, 1);
    const text = button.firstChild;
    text.textContent = 'Added to your box ';
    clearTimeout(button.resetTimer);
    button.resetTimer = setTimeout(() => { text.textContent = 'Add to my box '; }, 1400);
  }));
  items.addEventListener('click', event => {
    const button = event.target.closest('button[data-product]');
    if (button) change(button.dataset.product, Number(button.dataset.delta));
  });
  previewButton.addEventListener('click', () => {
    const preview = document.querySelector('#preview-items');
    preview.replaceChildren();
    for (const product of products) {
      const quantity = counts.get(product.id);
      if (!quantity) continue;
      const row = document.createElement('div');
      row.className = 'preview-row';
      const name = document.createElement('span');
      name.textContent = `${quantity} × ${product.name}`;
      const price = document.createElement('span');
      price.textContent = money(quantity * product.price);
      row.append(name, price);
      preview.append(row);
    }
    document.querySelector('.preview-total').textContent = `Your happy total: ${money(total())}`;
    dialog.showModal();
  });
  document.querySelectorAll('.dialog-close, .dialog-done').forEach(button => button.addEventListener('click', () => dialog.close()));
  dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  });
  render();
})();
